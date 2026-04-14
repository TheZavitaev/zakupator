"""Runtime configuration loaded from environment / .env file."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """App-wide settings. Reads from .env by default.

    Override any value via environment variable (same name, upper case).
    """

    telegram_bot_token: str = Field(..., description="BotFather token")
    database_url: str = Field(
        default="sqlite+aiosqlite:///./zakupator.db",
        description="SQLAlchemy async DB URL",
    )
    log_level: str = Field(default="INFO")

    # Default delivery address used before the user sets their own.
    # Moscow center so Tier 0 services serve real data from the start.
    default_address_label: str = "Москва (по умолчанию)"
    default_address_text: str = "Москва, Красная площадь"
    default_address_lat: float = 55.7539
    default_address_lon: float = 37.6208

    model_config = SettingsConfigDict(
        env_file=str(Path(__file__).resolve().parents[2] / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


def load_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
