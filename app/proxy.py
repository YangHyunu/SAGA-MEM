import asyncio
import hashlib
import json
from collections.abc import AsyncGenerator

import structlog
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from langgraph.store.memory import InMemoryStore
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.config import settings
from app.extractor import extract_episodes
from app.llm_client import LLMClient
from app.memory import render_recall, search_episodes
from app.schemas import ChatCompletionRequest

logger = structlog.get_logger()

router = APIRouter()
limiter = Limiter(key_func=get_remote_address)

MARKERS = {
    "world_state": "<!-- MEMORY_ENGINE:WORLD_STATE -->",
    "episode_recall": "<!-- MEMORY_ENGINE:EPISODE_RECALL -->",
    "narrative_cue": "<!-- MEMORY_ENGINE:NARRATIVE_CUE -->",
}

# Turn counter per card_id (in-memory, reset on restart)
_turn_counts: dict[str, int] = {}


def _find_injection_index(messages: list[dict]) -> int:
    """Find slot 6 position: last system message before trailing chat history."""
    for i in range(len(messages) - 1, -1, -1):
        if messages[i]["role"] == "system":
            return i + 1
    return len(messages)


def _inject_memory(
    messages: list[dict],
    *,
    episode_recall: str = "",
    world_state: str = "",
    narrative_cue: str = "",
) -> list[dict]:
    """Inject memory blocks into messages via markers or fallback slot 6.

    Supports 3 placeholder markers:
    - <!-- MEMORY_ENGINE:WORLD_STATE -->
    - <!-- MEMORY_ENGINE:EPISODE_RECALL -->
    - <!-- MEMORY_ENGINE:NARRATIVE_CUE -->

    If no markers found, combines all blocks and injects at slot 6.
    """
    if not any([episode_recall, world_state, narrative_cue]):
        return messages

    messages = [dict(m) for m in messages]

    # Try marker-based injection
    marker_found = False
    replacements = {
        MARKERS["world_state"]: world_state,
        MARKERS["episode_recall"]: episode_recall,
        MARKERS["narrative_cue"]: narrative_cue,
    }

    for msg in messages:
        content = msg.get("content", "")
        if not isinstance(content, str):
            continue
        for marker, text in replacements.items():
            if marker in content:
                msg["content"] = content.replace(marker, text or "")
                content = msg["content"]
                marker_found = True

    if marker_found:
        return messages

    # Fallback: combine all blocks and inject at slot 6
    blocks = []
    if world_state:
        blocks.append(world_state)
    if episode_recall:
        blocks.append(episode_recall)
    if narrative_cue:
        blocks.append(narrative_cue)

    combined = "\n".join(blocks)
    idx = _find_injection_index(messages)
    messages.insert(idx, {"role": "system", "content": combined})
    return messages


def _extract_user_id(request_body: dict) -> str:
    """Extract user_id from request, fallback to 'default'."""
    return request_body.get("user", "default")


def _extract_card_id(messages: list[dict]) -> str:
    """Extract card_id from first system message content hash.

    Uses hashlib for deterministic hash across restarts.
    """
    for msg in messages:
        if msg.get("role") == "system" and msg.get("content"):
            content = str(msg["content"])[:200]
            return hashlib.sha256(content.encode()).hexdigest()[:8]
    return "default"


def _get_last_user_message(messages: list[dict]) -> str:
    """Get the last user message as search query."""
    for msg in reversed(messages):
        if msg["role"] == "user" and msg.get("content"):
            content = msg["content"]
            if isinstance(content, str):
                return content[:500]
    return ""


@router.post("/v1/chat/completions", response_model=None)
@limiter.limit("60/minute")
async def chat_completions(request: Request):
    """OpenAI-compatible chat completions with multi-provider routing.

    Flow:
    1. Parse request
    2. Search episode memory
    3. Inject recall into messages (slot 6)
    4. Route to correct provider (Anthropic/Google/OpenAI) via LLMClient
    5. Stream response back to RisuAI in OpenAI SSE format
    6. Async: extract episodes from conversation
    """
    body = await request.json()
    parsed = ChatCompletionRequest.model_validate(body)

    user_id = _extract_user_id(body)
    card_id = _extract_card_id([m.model_dump() for m in parsed.messages])
    query = _get_last_user_message([m.model_dump() for m in parsed.messages])

    store: InMemoryStore = request.app.state.store
    llm_client: LLMClient = request.app.state.llm_client

    # Search episodes
    episodes = await search_episodes(
        store,
        user_id=user_id,
        card_id=card_id,
        query=query,
    )
    recall_text = render_recall(episodes)

    # Track turns
    turn_count = _turn_counts.get(card_id, 0)
    _turn_counts[card_id] = turn_count + 1

    # Inject memory into messages
    raw_messages = [m.model_dump(exclude_none=True) for m in parsed.messages]
    injected_messages = _inject_memory(
        raw_messages,
        episode_recall=recall_text,
    )

    provider = llm_client.detect_provider(parsed.model)

    logger.info(
        "proxy_request",
        user_id=user_id,
        card_id=card_id,
        model=parsed.model,
        provider=provider,
        stream=parsed.stream,
        episodes_recalled=len(episodes),
        original_messages=len(parsed.messages),
        injected_messages=len(injected_messages),
    )

    # Build LLM call kwargs
    llm_kwargs: dict = {}
    if parsed.temperature is not None:
        llm_kwargs["temperature"] = parsed.temperature
    if parsed.top_p is not None:
        llm_kwargs["top_p"] = parsed.top_p
    if parsed.stop is not None:
        llm_kwargs["stop"] = parsed.stop

    max_tokens = parsed.max_tokens or parsed.max_completion_tokens or 8192

    if parsed.stream:
        return StreamingResponse(
            _stream_and_extract(
                llm_client=llm_client,
                model=parsed.model,
                messages=injected_messages,
                max_tokens=max_tokens,
                user_id=user_id,
                card_id=card_id,
                app=request.app,
                **llm_kwargs,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # Non-streaming
    try:
        response = await llm_client.call(
            parsed.model,
            injected_messages,
            max_tokens=max_tokens,
            **llm_kwargs,
        )
    except Exception:
        logger.exception("llm_call_failed", model=parsed.model, provider=provider)
        return JSONResponse(
            content={"error": {"message": "LLM call failed", "type": "server_error"}},
            status_code=502,
        )

    # Trigger async extraction
    assistant_content = ""
    try:
        assistant_content = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        logger.warning("assistant_content_extract_failed")

    _trigger_extraction(
        app=request.app,
        messages=injected_messages,
        assistant_content=assistant_content,
        user_id=user_id,
        card_id=card_id,
    )

    return JSONResponse(content=response)


async def _stream_and_extract(
    *,
    llm_client: LLMClient,
    model: str,
    messages: list[dict],
    max_tokens: int,
    user_id: str,
    card_id: str,
    app: FastAPI,
    **kwargs,
) -> AsyncGenerator[bytes, None]:
    """Stream from LLMClient, collect response, trigger extraction."""
    collected_chunks: list[str] = []

    try:
        async for sse_bytes in llm_client.stream(
            model, messages, max_tokens=max_tokens, **kwargs,
        ):
            yield sse_bytes

            # Side-channel: collect content for extraction
            try:
                line = sse_bytes.decode("utf-8", errors="replace").strip()
                if line.startswith("data: ") and line != "data: [DONE]":
                    chunk = json.loads(line[6:])
                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                    content = delta.get("content", "")
                    if content:
                        collected_chunks.append(content)
            except (json.JSONDecodeError, IndexError, KeyError):
                pass
    except Exception:
        logger.exception("llm_stream_failed", model=model)
        error = json.dumps({"error": {"message": "LLM stream failed", "type": "server_error"}})
        yield ("data: " + error + "\n\n").encode()
        yield b"data: [DONE]\n\n"
        return

    # Trigger extraction
    assistant_content = "".join(collected_chunks)
    if assistant_content:
        _trigger_extraction(
            app=app,
            messages=messages,
            assistant_content=assistant_content,
            user_id=user_id,
            card_id=card_id,
        )

    logger.info(
        "proxy_stream_complete",
        user_id=user_id,
        card_id=card_id,
        assistant_length=len(assistant_content),
    )


def _trigger_extraction(
    *,
    app: FastAPI,
    messages: list[dict],
    assistant_content: str,
    user_id: str,
    card_id: str,
) -> None:
    """Trigger async episode extraction after response."""
    if not assistant_content:
        return

    executor = getattr(app.state, "executor", None)
    if executor is None:
        return

    full_messages = messages + [
        {"role": "assistant", "content": assistant_content}
    ]

    asyncio.create_task(
        extract_episodes(
            executor,
            messages=full_messages,
            user_id=user_id,
            card_id=card_id,
        )
    )
