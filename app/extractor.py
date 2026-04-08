from __future__ import annotations

import os
from typing import Any

from langmem import ReflectionExecutor, create_memory_store_manager
from langmem.utils import NamespaceTemplate
from langgraph.store.memory import InMemoryStore
import structlog

from app.config import settings
from app.schemas import RPEpisode

logger = structlog.get_logger()

# Namespace: (user_id, card_id, "episodes")
EPISODE_NS = NamespaceTemplate(("{user_id}", "{card_id}", "episodes"))


def init_extractor(
    store: InMemoryStore,
) -> ReflectionExecutor | None:
    """Initialize the async episode extraction pipeline.

    Uses create_memory_store_manager for stateful extraction + storage,
    wrapped in ReflectionExecutor for background processing.

    Returns None if no extraction model API key is available.
    """
    model = settings.extraction_model
    has_key = False
    if model.startswith("google_genai:"):
        has_key = bool(os.environ.get("GOOGLE_API_KEY"))
    else:
        has_key = bool(os.environ.get("OPENAI_API_KEY"))

    if not has_key:
        logger.warning(
            "extractor_skipped",
            reason="No API key for extraction model, episode extraction disabled",
            model=model,
        )
        return None

    manager = create_memory_store_manager(
        settings.extraction_model,
        schemas=[RPEpisode],
        namespace=EPISODE_NS,
        enable_inserts=True,
        enable_deletes=False,
        store=store,
        instructions=(
            "Extract RP episodes from the conversation. "
            "Focus on: scene changes, combat, relationship shifts, "
            "world state changes, and emotionally significant moments. "
            "Rate importance 0.9+ for world changes/death, "
            "0.7+ for relationship/combat, 0.4+ for daily events, "
            "0.1+ for trivial chat."
        ),
    )

    executor = ReflectionExecutor(manager, store=store)
    logger.info(
        "extractor_initialized",
        model=settings.extraction_model,
        schema="RPEpisode",
    )
    return executor


async def extract_episodes(
    executor: Any,
    *,
    messages: list[dict],
    user_id: str,
    card_id: str,
) -> None:
    """Submit messages for async background episode extraction.

    Non-blocking: returns immediately, extraction happens in background.
    Failures are logged but do not affect the main response.
    """
    try:
        config = {
            "configurable": {
                "user_id": user_id,
                "card_id": card_id,
            }
        }
        executor.submit(messages, config=config)
        logger.info(
            "episode_extraction_submitted",
            user_id=user_id,
            card_id=card_id,
            message_count=len(messages),
        )
    except Exception:
        logger.exception(
            "episode_extraction_submit_failed",
            user_id=user_id,
            card_id=card_id,
        )
