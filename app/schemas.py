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


# ── Character State (Profile Memory) ────────────────────────────


class CharacterState(BaseModel):
    """Dynamic NPC state. Uses enable_inserts=False (update only)."""

    name: str = Field(description="Character canonical name")
    hp: int = Field(default=100, description="Hit points")
    location: str = Field(default="unknown", description="Current location")
    emotional_state: str = Field(default="neutral", description="Current emotion")
    active_effects: list[str] = Field(default_factory=list, description="Active buffs/debuffs")
    last_action: str = Field(default="", description="Last significant action")


# ── Relationship ────────────────────────────────────────────────


class Relationship(BaseModel):
    """Dynamic relationship between two characters."""

    source: str = Field(description="Character A")
    target: str = Field(description="Character B")
    relation_type: str = Field(description="ally, enemy, romantic, neutral, etc.")
    trust_level: float = Field(
        ge=-1.0, le=1.0,
        description="-1.0 (hostile) to 1.0 (fully trusted)",
    )
    key_events: list[str] = Field(
        default_factory=list,
        description="Recent relationship-changing events (max 5)",
    )


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
