import os

from langgraph.store.memory import InMemoryStore
import structlog

from app.config import settings

logger = structlog.get_logger()


def init_store() -> InMemoryStore:
    """Initialize InMemoryStore for episode storage.

    Phase 1: InMemoryStore (dev/test, lost on restart).
    Phase 4: AsyncPostgresStore (production).

    If OPENAI_API_KEY is not set, creates store without embedding index
    (vector search disabled, but put/get still work).
    """
    has_openai_key = bool(os.environ.get("OPENAI_API_KEY"))

    if has_openai_key:
        store = InMemoryStore(
            index={
                "dims": 1536,
                "embed": "openai:text-embedding-3-small",
            }
        )
        logger.info("store_initialized", backend="in_memory", dims=1536, embeddings=True)
    else:
        store = InMemoryStore()
        logger.warning(
            "store_initialized_without_embeddings",
            backend="in_memory",
            reason="OPENAI_API_KEY not set, vector search disabled",
        )

    return store


async def search_episodes(
    store: InMemoryStore,
    *,
    user_id: str,
    card_id: str,
    query: str,
    limit: int | None = None,
) -> list[dict]:
    """Search episodes by vector similarity.

    Phase 1: basic vector search only.
    Phase 2: 4-stage RRF (Recent + Important + Similar + Character).
    """
    if limit is None:
        limit = settings.episode_recall_limit

    namespace = (user_id, card_id, "episodes")

    try:
        results = await store.asearch(
            namespace,
            query=query,
            limit=limit,
        )
        logger.info(
            "episode_search_complete",
            user_id=user_id,
            card_id=card_id,
            results_count=len(results),
        )
        return [item.value for item in results]
    except Exception:
        logger.exception(
            "episode_search_failed",
            user_id=user_id,
            card_id=card_id,
        )
        return []


def render_recall(episodes: list[dict]) -> str:
    """Render recalled episodes into text for slot 6 injection.

    Format:
      [Roleplay Summary - Memory Engine]
      <episode_recall>
      EP#1 (Turn 3-5): summary (importance: 0.8)
      ...
      </episode_recall>
    """
    if not episodes:
        return ""

    lines = ["[Roleplay Summary - Memory Engine]", "<episode_recall>"]

    for i, ep in enumerate(episodes, 1):
        turn_range = ep.get("turn_range", (0, 0))
        observation = ep.get("observation", "")
        importance = ep.get("importance", 0.0)
        participants = ", ".join(ep.get("participants", []))

        lines.append(
            f"EP#{i} (Turn {turn_range[0]}-{turn_range[1]}): "
            f"{observation} [{participants}] (importance: {importance:.1f})"
        )

    lines.append("</episode_recall>")
    return "\n".join(lines)
