import asyncio
import hashlib
import json
from collections.abc import AsyncGenerator

import httpx
import structlog
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from langgraph.store.memory import InMemoryStore
from slowapi import Limiter
from slowapi.util import get_remote_address
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import settings
from app.extractor import extract_episodes
from app.memory import render_recall, search_episodes
from app.schemas import ChatCompletionRequest

logger = structlog.get_logger()

router = APIRouter()
limiter = Limiter(key_func=get_remote_address)

MEMORY_MARKER = "<!-- MEMORY_ENGINE:EPISODE_RECALL -->"


def _find_injection_index(messages: list[dict]) -> int:
    """Find the index to inject memory (slot 6 position).

    Strategy:
    1. If placeholder marker exists, use that position.
    2. Otherwise, find the last system message before the trailing
       chat history block (contiguous user/assistant at the tail).
    """
    for i, msg in enumerate(messages):
        if msg.get("content") and MEMORY_MARKER in str(msg["content"]):
            return i

    # Walk backward: find the start of the trailing chat history block
    for i in range(len(messages) - 1, -1, -1):
        if messages[i]["role"] == "system":
            return i + 1

    return len(messages)


def _inject_memory(messages: list[dict], recall_text: str) -> list[dict]:
    """Inject memory recall into the messages array at slot 6."""
    if not recall_text:
        return messages

    messages = [dict(m) for m in messages]

    # Check for placeholder marker first
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str) and MEMORY_MARKER in content:
            msg["content"] = content.replace(MEMORY_MARKER, recall_text)
            return messages

    # Fallback: insert as new system message
    idx = _find_injection_index(messages)
    memory_msg = {"role": "system", "content": recall_text}
    messages.insert(idx, memory_msg)
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
    """Reverse proxy for OpenAI-compatible chat completions.

    Flow:
    1. Parse request
    2. Search episode memory
    3. Inject recall into messages (slot 6)
    4. Forward to upstream LLM via httpx SSE
    5. Stream response back to RisuAI
    6. Async: extract episodes from conversation
    """
    body = await request.json()
    parsed = ChatCompletionRequest.model_validate(body)

    user_id = _extract_user_id(body)
    card_id = _extract_card_id([m.model_dump() for m in parsed.messages])
    query = _get_last_user_message([m.model_dump() for m in parsed.messages])

    store: InMemoryStore = request.app.state.store

    # Search episodes
    episodes = await search_episodes(
        store,
        user_id=user_id,
        card_id=card_id,
        query=query,
    )
    recall_text = render_recall(episodes)

    # Inject memory into messages
    raw_messages = [m.model_dump(exclude_none=True) for m in parsed.messages]
    injected_messages = _inject_memory(raw_messages, recall_text)

    # Build upstream request body
    upstream_body = body.copy()
    upstream_body["messages"] = injected_messages

    logger.info(
        "proxy_request",
        user_id=user_id,
        card_id=card_id,
        model=parsed.model,
        stream=parsed.stream,
        episodes_recalled=len(episodes),
        original_messages=len(parsed.messages),
        injected_messages=len(injected_messages),
    )

    client: httpx.AsyncClient = request.app.state.http_client
    upstream_url = settings.upstream_base_url + "/chat/completions"

    # Forward Authorization header from RisuAI, fallback to configured key
    auth_header = request.headers.get("authorization", "")
    if not auth_header and settings.openai_api_key:
        auth_header = "Bearer " + settings.openai_api_key

    headers = {
        "Authorization": auth_header,
        "Content-Type": "application/json",
    }

    if parsed.stream:
        return StreamingResponse(
            _stream_upstream(
                client=client,
                url=upstream_url,
                headers=headers,
                body=upstream_body,
                messages=injected_messages,
                user_id=user_id,
                card_id=card_id,
                app=request.app,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # Non-streaming: forward with retry
    resp = await _post_upstream(client, upstream_url, headers, upstream_body)

    # Trigger async extraction
    _trigger_extraction(
        app=request.app,
        messages=injected_messages,
        assistant_content=_extract_assistant_from_response(resp.json()),
        user_id=user_id,
        card_id=card_id,
    )

    return JSONResponse(
        content=resp.json(),
        status_code=resp.status_code,
    )


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    reraise=True,
)
async def _post_upstream(
    client: httpx.AsyncClient,
    url: str,
    headers: dict,
    body: dict,
) -> httpx.Response:
    """POST to upstream with tenacity retry on transient errors."""
    resp = await client.post(url, headers=headers, json=body, timeout=120.0)
    if resp.status_code >= 500:
        logger.warning(
            "upstream_server_error",
            status=resp.status_code,
            body=resp.text[:200],
        )
        resp.raise_for_status()
    return resp


async def _stream_upstream(
    *,
    client: httpx.AsyncClient,
    url: str,
    headers: dict,
    body: dict,
    messages: list[dict],
    user_id: str,
    card_id: str,
    app: FastAPI,
) -> AsyncGenerator[bytes, None]:
    """Stream SSE from upstream LLM, collecting full response.

    Uses aiter_bytes() for raw byte passthrough to preserve SSE framing.
    Checks upstream status before streaming; yields error body on non-2xx.
    """
    collected_chunks: list[str] = []
    line_buffer = b""

    async with client.stream(
        "POST",
        url,
        headers=headers,
        json=body,
        timeout=120.0,
    ) as resp:
        # Propagate upstream errors instead of masking as 200
        if resp.status_code != 200:
            error_body = await resp.aread()
            logger.warning(
                "upstream_error_in_stream",
                status=resp.status_code,
                body=error_body[:200].decode("utf-8", errors="replace"),
            )
            yield error_body
            return

        async for raw_chunk in resp.aiter_bytes():
            # Forward raw bytes to client (preserves SSE framing)
            yield raw_chunk

            # Side-channel: parse SSE lines for content extraction
            line_buffer += raw_chunk
            while b"\n" in line_buffer:
                line_bytes, line_buffer = line_buffer.split(b"\n", 1)
                line = line_bytes.decode("utf-8", errors="replace").rstrip("\r")

                if line.startswith("data: ") and line != "data: [DONE]":
                    try:
                        chunk = json.loads(line[6:])
                        delta = chunk.get("choices", [{}])[0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            collected_chunks.append(content)
                    except (json.JSONDecodeError, IndexError, KeyError):
                        logger.warning("sse_chunk_parse_failed", raw_line=line[:100])

    # After stream completes, trigger async extraction
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

    # Build full conversation including assistant response
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


def _extract_assistant_from_response(response: dict) -> str:
    """Extract assistant content from non-streaming response."""
    try:
        return response["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        logger.warning("assistant_content_extract_failed", response_keys=list(response.keys()))
        return ""
