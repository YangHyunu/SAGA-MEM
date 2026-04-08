import os

from pydantic import Field
from pydantic_settings import BaseSettings
import structlog

logger = structlog.get_logger()


class Settings(BaseSettings):
    """RP Memory Engine configuration."""

    model_config = {"env_prefix": "SAGA_", "env_file": ".env", "env_file_encoding": "utf-8"}

    # Upstream LLM API
    upstream_base_url: str = Field(
        default="https://api.openai.com/v1",
        description="Base URL for upstream LLM API",
    )
    openai_api_key: str = Field(
        default="",
        description="OpenAI API key (embeddings + fallback extraction)",
    )
    google_api_key: str = Field(
        default="",
        description="Google API key (Gemini extraction, free tier)",
    )

    # Memory engine
    extraction_model: str = Field(
        default="google_genai:gemini-2.5-flash-preview-04-17",
        description="Model for episode extraction (Flash-tier)",
    )
    token_budget: int = Field(
        default=3800,
        description="Max tokens for memory injection (~3% of 128K)",
    )
    episode_recall_limit: int = Field(
        default=5,
        description="Max episodes to recall per turn",
    )
    curation_interval: int = Field(
        default=10,
        description="Run curator every N turns",
    )

    # Server
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8000)


settings = Settings()

# Sync API keys so langmem/langchain pick them up automatically
if settings.openai_api_key and not os.environ.get("OPENAI_API_KEY"):
    os.environ["OPENAI_API_KEY"] = settings.openai_api_key
if settings.google_api_key and not os.environ.get("GOOGLE_API_KEY"):
    os.environ["GOOGLE_API_KEY"] = settings.google_api_key
