"""Runtime configuration loaded from environment variables.

Single source of truth for settings across services. Each service imports
`settings` and uses the fields it needs; fields are optional where a
service may legitimately run without the credential (e.g. `ingestion`
does not need `ANTHROPIC_API_KEY`).
"""

from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Database
    database_url: str = Field(
        default="postgresql+psycopg://tbc:tbc@localhost:5432/tbc",
        validation_alias="TBC_DATABASE_URL",
    )

    # Ollama
    ollama_base_url: str = Field(
        default="http://localhost:11434", validation_alias="TBC_OLLAMA_BASE_URL"
    )
    understanding_model: str = Field(
        default="qwen2.5:7b-instruct-q4_K_M", validation_alias="TBC_UNDERSTANDING_MODEL"
    )
    embedding_model: str = Field(default="bge-m3", validation_alias="TBC_EMBEDDING_MODEL")

    # Anthropic (brief/weekly/agent when TBC_LLM_PROVIDER=anthropic)
    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    brief_model: str = Field(default="claude-sonnet-4-6", validation_alias="TBC_BRIEF_MODEL")

    # DeepSeek (brief/weekly when TBC_LLM_PROVIDER=deepseek)
    deepseek_api_key: SecretStr | None = Field(default=None, validation_alias="DEEPSEEK_API_KEY")

    # novita.ai (brief/weekly/agent when TBC_LLM_PROVIDER=novita)
    novita_api_key: SecretStr | None = Field(default=None, validation_alias="NOVITA_API_KEY")
    novita_model: str = Field(default="google/gemma-3-27b-it", validation_alias="TBC_NOVITA_MODEL")

    # Provider selection for brief + weekly workers and the tg-bot agent.
    # Valid values: "anthropic", "deepseek", "novita"
    llm_provider: str = Field(default="deepseek", validation_alias="TBC_LLM_PROVIDER")

    # Telegram userbot
    tg_api_id: int | None = Field(default=None, validation_alias="TBC_TG_API_ID")
    tg_api_hash: SecretStr | None = Field(default=None, validation_alias="TBC_TG_API_HASH")
    tg_session_path: str = Field(
        default="./tbc.session", validation_alias="TBC_TG_SESSION_PATH"
    )

    # Telegram bot
    tg_bot_token: SecretStr | None = Field(default=None, validation_alias="TBC_TG_BOT_TOKEN")
    tg_owner_user_id: int | None = Field(default=None, validation_alias="TBC_TG_OWNER_USER_ID")
    # Telegram @username of the owner (without the @). Used by the auto-tagger
    # to detect mentions of the user in messages from others, so chats where
    # the owner has no involvement at all can be skipped.
    tg_owner_username: str | None = Field(
        default="gokhanseckin", validation_alias="TBC_TG_OWNER_USERNAME"
    )

    # Brief scheduling
    brief_tz: str = Field(default="Europe/Istanbul", validation_alias="TBC_BRIEF_TZ")
    brief_time: str = Field(default="07:00", validation_alias="TBC_BRIEF_TIME")
    weekly_day: str = Field(default="Monday", validation_alias="TBC_WEEKLY_DAY")
    weekly_time: str = Field(default="08:00", validation_alias="TBC_WEEKLY_TIME")
    # When TBC_UNDERSTANDING_MODE=brief-coupled the worker only runs the LLM
    # pass on demand (triggered by the brief). When =continuous it keeps the
    # legacy 5-second polling loop. The brief worker waits up to
    # TBC_BRIEF_PRE_UNDERSTANDING_TIMEOUT_S seconds for the queue to drain
    # before generating the brief.
    understanding_mode: str = Field(
        default="brief-coupled", validation_alias="TBC_UNDERSTANDING_MODE"
    )
    brief_pre_understanding_timeout_s: int = Field(
        default=300, validation_alias="TBC_BRIEF_PRE_UNDERSTANDING_TIMEOUT_S"
    )

    # MCP server
    mcp_bearer_token: SecretStr | None = Field(default=None, validation_alias="TBC_MCP_BEARER_TOKEN")
    mcp_public_url: str = Field(
        default="", validation_alias="TBC_MCP_PUBLIC_URL"
    )

    # Chat auto-tagger
    tagger_interval_seconds: int = Field(
        default=3600, validation_alias="TBC_TAGGER_INTERVAL_SECONDS"
    )
    tagger_auto_threshold: float = Field(
        default=0.78, validation_alias="TBC_TAGGER_AUTO_THRESHOLD"
    )
    tagger_margin: float = Field(
        default=0.05, validation_alias="TBC_TAGGER_MARGIN"
    )
    tagger_min_messages: int = Field(
        default=10, validation_alias="TBC_TAGGER_MIN_MESSAGES"
    )
    tagger_sample_size: int = Field(
        default=50, validation_alias="TBC_TAGGER_SAMPLE_SIZE"
    )
    tagger_max_per_run: int = Field(
        default=200, validation_alias="TBC_TAGGER_MAX_PER_RUN"
    )

    # DM router (Stage 2 — local-first intent classification)
    router_model: str = Field(
        default="qwen2.5:3b-instruct-q4_K_M",
        validation_alias="TBC_ROUTER_MODEL",
    )
    router_min_confidence: float = Field(
        default=0.7, validation_alias="TBC_ROUTER_MIN_CONFIDENCE"
    )

    # Logging
    log_level: str = Field(default="INFO", validation_alias="TBC_LOG_LEVEL")


settings = Settings()
