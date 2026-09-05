"""Ops API settings (local Mongo, admin token, optional alert webhook)."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """FastAPI ops process config. Secrets stay in env / .env."""

    model_config = SettingsConfigDict(
        env_prefix="VITUAL_",
        env_file=".env",
        extra="ignore",
    )

    mongo_uri: str = "mongodb://127.0.0.1:27018"
    mongo_db: str = "vitual"
    admin_token: str = "local-admin"
    alert_webhook_url: str = ""
    host: str = "127.0.0.1"
    port: int = 8901
    cors_origins: str = "http://127.0.0.1:3001,http://localhost:3001,http://127.0.0.1:3000,http://localhost:3000"
    # Asia/Shanghai daily discover then batch (limit 1)
    schedule_discover: str = "0 8 * * *"
    schedule_batch: str = "30 8 * * *"
    schedule_enabled: bool = True
    schedule_game_claim: str = "0 22 * * 4"
    game_claim_enabled: bool = False
    game_claim_stores: str = "epic,steam"
    game_claim_profile_dir: str = "data/game_claim/browser"
    game_claim_artifacts_dir: str = "data/game_claim/runs"
    game_claim_headless: bool = True
    game_claim_slow_mo_ms: int = 0
    game_claim_browser_channel: str = "chromium"
    game_claim_profile_name: str = ""
    game_claim_epic_urls: str = ""
    game_claim_steam_urls: str = ""


def get_settings() -> Settings:
    return Settings()
