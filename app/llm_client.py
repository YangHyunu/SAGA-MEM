"""Multi-provider LLM client for SAGA-MEM.

Routes requests to Anthropic, Google, or OpenAI based on model name.
Converts OpenAI-format messages to each provider's native format.
Returns responses in OpenAI-compatible SSE format for RisuAI.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import settings

logger = structlog.get_logger()


class LLMClient:
    """Multi-provider LLM client (Anthropic, Google, OpenAI)."""

    def __init__(self) -> None:
        self._anthropic: Any | None = None
        self._openai: Any | None = None
        self._google: Any | None = None

    async def init(self) -> None:
        """Initialize SDK clients based on available API keys."""
        import os

        if os.environ.get("ANTHROPIC_API_KEY"):
            import anthropic
            self._anthropic = anthropic.AsyncAnthropic()
            logger.info("llm_client_provider_ready", provider="anthropic")

        if os.environ.get("OPENAI_API_KEY"):
            import openai
            self._openai = openai.AsyncOpenAI()
            logger.info("llm_client_provider_ready", provider="openai")

        if os.environ.get("GOOGLE_API_KEY"):
            from google import genai
            self._google = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
            logger.info("llm_client_provider_ready", provider="google")

    async def close(self) -> None:
        """Cleanup SDK clients."""
        if self._anthropic and hasattr(self._anthropic, "close"):
            await self._anthropic.close()
        if self._openai and hasattr(self._openai, "close"):
            await self._openai.close()

    def detect_provider(self, model: str) -> str:
        """Detect provider from model name string."""
        model_lower = model.lower()
        if "claude" in model_lower or "anthropic" in model_lower:
            return "anthropic"
        if "gemini" in model_lower or "google" in model_lower:
            return "google"
        return "openai"

    # ── Non-streaming ──────────────────────────────────────────

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    async def call(
        self,
        model: str,
        messages: list[dict],
        *,
        temperature: float | None = None,
        max_tokens: int = 8192,
        stream: bool = False,
        **kwargs,
    ) -> dict:
        """Call LLM and return OpenAI-compatible response dict."""
        provider = self.detect_provider(model)
        temp = temperature if temperature is not None else 0.7

        if provider == "anthropic":
            text = await self._call_anthropic(model, messages, temp, max_tokens, **kwargs)
        elif provider == "google":
            text = await self._call_google(model, messages, temp, max_tokens, **kwargs)
        else:
            text = await self._call_openai(model, messages, temp, max_tokens, **kwargs)

        return self._wrap_response(model, text)

    # ── Streaming ──────────────────────────────────────────────

    async def stream(
        self,
        model: str,
        messages: list[dict],
        *,
        temperature: float | None = None,
        max_tokens: int = 8192,
        **kwargs,
    ) -> AsyncGenerator[bytes, None]:
        """Stream LLM response as OpenAI-compatible SSE bytes."""
        provider = self.detect_provider(model)
        temp = temperature if temperature is not None else 0.7
        chunk_id = "chatcmpl-saga-" + uuid.uuid4().hex[:12]

        if provider == "anthropic":
            gen = self._stream_anthropic(model, messages, temp, max_tokens, **kwargs)
        elif provider == "google":
            gen = self._stream_google(model, messages, temp, max_tokens, **kwargs)
        else:
            gen = self._stream_openai(model, messages, temp, max_tokens, **kwargs)

        async for text_chunk in gen:
            sse_data = {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": {"content": text_chunk}, "finish_reason": None}],
            }
            yield ("data: " + json.dumps(sse_data) + "\n\n").encode()

        # Send finish chunk
        finish_data = {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        yield ("data: " + json.dumps(finish_data) + "\n\n").encode()
        yield b"data: [DONE]\n\n"

    # ── Message preparation ────────────────────────────────────

    @staticmethod
    def _to_text(content: Any) -> str:
        """Extract plain text from content (string or multimodal array)."""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
            )
        return str(content) if content else ""

    @staticmethod
    def _prepare_anthropic_messages(messages: list[dict]) -> tuple[list[dict], list[dict]]:
        """Split into (system_parts, non_system) for Anthropic API."""
        system_parts: list[dict] = []
        non_system: list[dict] = []

        for msg in messages:
            if msg["role"] == "system":
                text = LLMClient._to_text(msg.get("content", ""))
                if text:
                    system_parts.append({"type": "text", "text": text})
            else:
                content = msg.get("content", "")
                entry = {"role": msg["role"], "content": LLMClient._to_text(content)}
                non_system.append(entry)

        return system_parts, non_system

    @staticmethod
    def _prepare_openai_messages(messages: list[dict]) -> list[dict]:
        """Merge multiple system messages into one for OpenAI."""
        merged: list[dict] = []
        system_parts: list[str] = []

        for msg in messages:
            if msg["role"] == "system":
                system_parts.append(LLMClient._to_text(msg.get("content", "")))
            else:
                merged.append({"role": msg["role"], "content": msg.get("content", "")})

        if system_parts:
            merged.insert(0, {"role": "system", "content": "\n\n".join(system_parts)})

        return merged

    @staticmethod
    def _prepare_google_messages(messages: list[dict]) -> tuple[list[dict], str | None]:
        """Convert to Gemini format: (contents, system_instruction)."""
        contents: list[dict] = []
        system_parts: list[str] = []

        for msg in messages:
            if msg["role"] == "system":
                system_parts.append(LLMClient._to_text(msg.get("content", "")))
            else:
                role = "user" if msg["role"] == "user" else "model"
                text = LLMClient._to_text(msg.get("content", ""))
                contents.append({"role": role, "parts": [{"text": text}]})

        system_instruction = "\n\n".join(system_parts) if system_parts else None
        return contents, system_instruction

    # ── Provider implementations ───────────────────────────────

    async def _call_anthropic(self, model: str, messages: list[dict], temperature: float, max_tokens: int, **kwargs) -> str:
        if not self._anthropic:
            raise RuntimeError("Anthropic API key not configured")

        # Strip provider prefix if present
        model_name = model.split("/")[-1] if "/" in model else model
        system_parts, non_system = self._prepare_anthropic_messages(messages)

        create_kwargs: dict[str, Any] = {
            "model": model_name,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": non_system,
        }
        if system_parts:
            create_kwargs["system"] = system_parts
        if kwargs.get("stop"):
            stop = kwargs["stop"]
            create_kwargs["stop_sequences"] = [stop] if isinstance(stop, str) else stop

        response = await self._anthropic.messages.create(**create_kwargs)
        return "".join(block.text for block in response.content if hasattr(block, "text"))

    async def _call_google(self, model: str, messages: list[dict], temperature: float, max_tokens: int, **kwargs) -> str:
        if not self._google:
            raise RuntimeError("Google API key not configured")

        from google import genai

        model_name = model.split("/")[-1] if "/" in model else model
        contents, system_instruction = self._prepare_google_messages(messages)

        gen_config: dict[str, Any] = {
            "temperature": temperature,
            "max_output_tokens": max_tokens,
        }
        if kwargs.get("stop"):
            stop = kwargs["stop"]
            gen_config["stop_sequences"] = [stop] if isinstance(stop, str) else stop
        if system_instruction:
            gen_config["system_instruction"] = system_instruction

        config_obj = genai.types.GenerateContentConfig(**gen_config)

        response = await asyncio.to_thread(
            self._google.models.generate_content,
            model=model_name,
            contents=contents,
            config=config_obj,
        )
        return response.text or ""

    async def _call_openai(self, model: str, messages: list[dict], temperature: float, max_tokens: int, **kwargs) -> str:
        if not self._openai:
            raise RuntimeError("OpenAI API key not configured")

        model_name = model.split("/")[-1] if "/" in model else model
        merged = self._prepare_openai_messages(messages)

        create_kwargs: dict[str, Any] = {
            "model": model_name,
            "messages": merged,
            "temperature": temperature,
            "max_completion_tokens": max_tokens,
        }
        if kwargs.get("stop"):
            create_kwargs["stop"] = kwargs["stop"]
        if kwargs.get("top_p") is not None:
            create_kwargs["top_p"] = kwargs["top_p"]

        response = await self._openai.chat.completions.create(**create_kwargs)
        return response.choices[0].message.content if response.choices else ""

    # ── Streaming implementations ──────────────────────────────

    async def _stream_anthropic(self, model: str, messages: list[dict], temperature: float, max_tokens: int, **kwargs) -> AsyncGenerator[str, None]:
        if not self._anthropic:
            raise RuntimeError("Anthropic API key not configured")

        model_name = model.split("/")[-1] if "/" in model else model
        system_parts, non_system = self._prepare_anthropic_messages(messages)

        create_kwargs: dict[str, Any] = {
            "model": model_name,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": non_system,
        }
        if system_parts:
            create_kwargs["system"] = system_parts

        async with self._anthropic.messages.stream(**create_kwargs) as stream:
            async for text in stream.text_stream:
                yield text

    async def _stream_google(self, model: str, messages: list[dict], temperature: float, max_tokens: int, **kwargs) -> AsyncGenerator[str, None]:
        """Google streaming (sync SDK, yields full result)."""
        result = await self._call_google(model, messages, temperature, max_tokens, **kwargs)
        yield result

    async def _stream_openai(self, model: str, messages: list[dict], temperature: float, max_tokens: int, **kwargs) -> AsyncGenerator[str, None]:
        if not self._openai:
            raise RuntimeError("OpenAI API key not configured")

        model_name = model.split("/")[-1] if "/" in model else model
        merged = self._prepare_openai_messages(messages)

        create_kwargs: dict[str, Any] = {
            "model": model_name,
            "messages": merged,
            "temperature": temperature,
            "max_completion_tokens": max_tokens,
            "stream": True,
        }

        stream = await self._openai.chat.completions.create(**create_kwargs)
        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    # ── Response wrapper ───────────────────────────────────────

    @staticmethod
    def _wrap_response(model: str, text: str) -> dict:
        """Wrap text response in OpenAI-compatible format."""
        return {
            "id": "chatcmpl-saga-" + uuid.uuid4().hex[:12],
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
