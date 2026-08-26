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
    port: int = 8800
    cors_origins: str = "http://127.0.0.1:3001,http://localhost:3001,http://127.0.0.1:3000,http://localhost:3000"
    # Asia/Shanghai daily discover then batch (limit 1)
    schedule_discover: str = "0 8 * * *"
    schedule_batch: str = "30 8 * * *"
    schedule_enabled: bool = True


def get_settings() -> Settings:
    return Settings()
