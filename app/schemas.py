from pydantic import BaseModel, Field


# ── Episode Memory ──────────────────────────────────────────────


class RPEpisode(BaseModel):
    """Single RP episode extracted from conversation turns."""

    observation: str = Field(description="Scene summary")
    participants: list[str] = Field(description="Characters present")
    scene_type: str = Field(description="combat, dialogue, exploration, emotional")
    location: str = Field(description="Scene location")
    emotional_tone: str = Field(description="Emotional tone of the scene")
    player_action: str = Field(description="Player action summary")
    consequence: str = Field(description="Result or world change")
    importance: float = Field(
        ge=0.0,
        le=1.0,
        description="0.0~1.0: 0.9+ world change/death, 0.7+ relationship/combat, 0.4+ daily, 0.1+ trivial",
    )
    turn_range: list[int] = Field(description="Turn range [start, end]", min_length=2, max_length=2)


# ── OpenAI-compatible Request/Response ──────────────────────────


class ChatMessage(BaseModel):
    """Single message in a chat completion request."""

    role: str
    content: str | list | None = None
    name: str | None = None


class ChatCompletionRequest(BaseModel):
    """OpenAI-compatible chat completion request."""

    model: str
    messages: list[ChatMessage]
    stream: bool = True
    temperature: float | None = None
    max_tokens: int | None = None
    max_completion_tokens: int | None = None
    top_p: float | None = None
    frequency_penalty: float | None = None
    presence_penalty: float | None = None
    stop: str | list[str] | None = None
    user: str | None = None

    model_config = {"extra": "allow"}
