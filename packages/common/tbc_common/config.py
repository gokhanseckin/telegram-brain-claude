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

    # Anthropic
    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    brief_model: str = Field(default="claude-sonnet-4-6", validation_alias="TBC_BRIEF_MODEL")

    # Telegram userbot
    tg_api_id: int | None = Field(default=None, validation_alias="TBC_TG_API_ID")
    tg_api_hash: SecretStr | None = Field(default=None, validation_alias="TBC_TG_API_HASH")
    tg_session_path: str = Field(
        default="./tbc.session", validation_alias="TBC_TG_SESSION_PATH"
    )

    # Telegram bot
    tg_bot_token: SecretStr | None = Field(default=None, validation_alias="TBC_TG_BOT_TOKEN")
    tg_owner_user_id: int | None = Field(default=None, validation_alias="TBC_TG_OWNER_USER_ID")

    # Brief scheduling
    brief_tz: str = Field(default="Europe/Istanbul", validation_alias="TBC_BRIEF_TZ")
    brief_time: str = Field(default="07:00", validation_alias="TBC_BRIEF_TIME")
    weekly_day: str = Field(default="Monday", validation_alias="TBC_WEEKLY_DAY")
    weekly_time: str = Field(default="08:00", validation_alias="TBC_WEEKLY_TIME")

    # MCP server
    mcp_bearer_token: SecretStr | None = Field(default=None, validation_alias="TBC_MCP_BEARER_TOKEN")
    mcp_public_url: str = Field(
        default="https://mcp.example.com", validation_alias="TBC_MCP_PUBLIC_URL"
    )

    # Logging
    log_level: str = Field(default="INFO", validation_alias="TBC_LOG_LEVEL")


settings = Settings()
