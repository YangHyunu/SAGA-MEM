import asyncio
import os

from langgraph.store.memory import InMemoryStore
import structlog

from app.config import settings

logger = structlog.get_logger()

# RRF weights (PLAN.md section 5)
RRF_K = 60
RRF_WEIGHTS = {
    "recent": 1.0,
    "important": 1.2,
    "similar": 0.8,
    "character": 0.6,
}


def init_store() -> InMemoryStore:
    """Initialize InMemoryStore for episode storage.

    Phase 1: InMemoryStore (dev/test, lost on restart).
    Phase 4: AsyncPostgresStore (production).
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


# ── 4-Stage Search ─────────────────────────────────────────────


async def _search_recent(
    store: InMemoryStore, namespace: tuple, n: int = 10,
) -> list[dict]:
    """Stage 1: Recent episodes by turn_range (most recent first)."""
    try:
        results = await store.asearch(namespace, query="", limit=50)
        items = [item.value for item in results]
        # Sort by turn_range end descending
        items.sort(
            key=lambda ep: ep.get("turn_range", [0, 0])[-1] if ep.get("turn_range") else 0,
            reverse=True,
        )
        return items[:n]
    except Exception:
        logger.exception("search_recent_failed")
        return []


async def _search_important(
    store: InMemoryStore, namespace: tuple, threshold: float = 0.7, n: int = 10,
) -> list[dict]:
    """Stage 2: Important episodes (importance >= threshold)."""
    try:
        results = await store.asearch(namespace, query="", limit=50)
        items = [item.value for item in results]
        important = [ep for ep in items if ep.get("importance", 0) >= threshold]
        important.sort(key=lambda ep: ep.get("importance", 0), reverse=True)
        return important[:n]
    except Exception:
        logger.exception("search_important_failed")
        return []


async def _search_similar(
    store: InMemoryStore, namespace: tuple, query: str, n: int = 15,
) -> list[dict]:
    """Stage 3: Vector similarity search."""
    if not query:
        return []
    try:
        results = await store.asearch(namespace, query=query, limit=n)
        return [item.value for item in results]
    except Exception:
        logger.exception("search_similar_failed")
        return []


async def _search_by_character(
    store: InMemoryStore, namespace: tuple, active_chars: list[str], n: int = 10,
) -> list[dict]:
    """Stage 4: Episodes mentioning active characters."""
    if not active_chars:
        return []
    try:
        results = await store.asearch(namespace, query="", limit=50)
        items = [item.value for item in results]
        matched = []
        for ep in items:
            participants = ep.get("participants", [])
            if any(char in participants for char in active_chars):
                matched.append(ep)
        return matched[:n]
    except Exception:
        logger.exception("search_by_character_failed")
        return []


def _extract_active_characters(query: str, messages: list[dict] | None = None) -> list[str]:
    """Extract character names mentioned in recent context.

    Simple heuristic: collect names from the last user message.
    Phase 3 will use NPC registry for better detection.
    """
    # For now, return empty — character search is a bonus stage
    # that gracefully returns [] when no chars are detected
    return []


def _rrf_merge(
    sources: dict[str, list[dict]],
    k: int = RRF_K,
    weights: dict[str, float] | None = None,
) -> list[dict]:
    """Reciprocal Rank Fusion across multiple search stages.

    Each episode gets: score = sum(weight / (k + rank)) for each source it appears in.
    """
    if weights is None:
        weights = RRF_WEIGHTS

    # Deduplicate by observation (episodes from different stages may overlap)
    scored: dict[str, tuple[float, dict]] = {}

    for source_name, source_list in sources.items():
        w = weights.get(source_name, 0.5)
        for rank, ep in enumerate(source_list):
            # Use observation as dedup key
            key = ep.get("observation", "")[:100]
            if not key:
                continue
            rrf_score = w / (k + rank)
            if key in scored:
                old_score, old_ep = scored[key]
                scored[key] = (old_score + rrf_score, old_ep)
            else:
                scored[key] = (rrf_score, ep)

    # Sort by RRF score descending
    sorted_items = sorted(scored.values(), key=lambda x: x[0], reverse=True)
    return [ep for _, ep in sorted_items]


def _estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars per token)."""
    return len(text) // 4


def _pack_within_budget(episodes: list[dict], max_tokens: int) -> list[dict]:
    """Pack episodes within token budget, highest RRF score first."""
    packed: list[dict] = []
    used_tokens = 0

    for ep in episodes:
        ep_text = ep.get("observation", "") + " ".join(ep.get("participants", []))
        ep_tokens = _estimate_tokens(ep_text)
        if used_tokens + ep_tokens > max_tokens:
            break
        packed.append(ep)
        used_tokens += ep_tokens

    return packed


async def search_episodes(
    store: InMemoryStore,
    *,
    user_id: str,
    card_id: str,
    query: str,
    limit: int | None = None,
    messages: list[dict] | None = None,
) -> list[dict]:
    """4-stage RRF episode search.

    Runs Recent, Important, Similar, Character stages in parallel,
    merges with RRF scoring, packs within token budget.
    """
    namespace = (user_id, card_id, "episodes")
    active_chars = _extract_active_characters(query, messages)

    # Run all 4 stages in parallel
    recent, important, similar, character = await asyncio.gather(
        _search_recent(store, namespace, n=10),
        _search_important(store, namespace, threshold=0.7, n=10),
        _search_similar(store, namespace, query=query, n=15),
        _search_by_character(store, namespace, active_chars=active_chars, n=10),
        return_exceptions=True,
    )

    # Handle partial failures gracefully
    sources: dict[str, list[dict]] = {}
    for name, result in [("recent", recent), ("important", important), ("similar", similar), ("character", character)]:
        if isinstance(result, list):
            sources[name] = result
        else:
            logger.warning("search_stage_failed", stage=name, error=str(result))
            sources[name] = []

    # RRF merge + token budget packing
    merged = _rrf_merge(sources)
    packed = _pack_within_budget(merged, max_tokens=settings.token_budget // 3)

    logger.info(
        "episode_search_complete",
        user_id=user_id,
        card_id=card_id,
        results_count=len(packed),
        stages={name: len(eps) for name, eps in sources.items()},
    )

    return packed


def render_recall(episodes: list[dict]) -> str:
    """Render recalled episodes into text for slot 6 injection."""
    if not episodes:
        return ""

    lines = ["[Roleplay Summary - Memory Engine]", "<episode_recall>"]

    for i, ep in enumerate(episodes, 1):
        turn_range = ep.get("turn_range", [0, 0])
        observation = ep.get("observation", "")
        importance = ep.get("importance", 0.0)
        participants = ", ".join(ep.get("participants", []))
        start = turn_range[0] if len(turn_range) > 0 else 0
        end = turn_range[1] if len(turn_range) > 1 else start

        lines.append(
            "EP#{} (Turn {}-{}): {} [{}] (importance: {:.1f})".format(
                i, start, end, observation, participants, importance,
            )
        )

    lines.append("</episode_recall>")
    return "\n".join(lines)
