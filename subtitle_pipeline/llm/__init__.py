"""Pluggable LLM backends for chat / translate roles."""

from .factory import complete, configure_llm, ensure_role_ready, get_client, get_role_config

__all__ = [
    "complete",
    "configure_llm",
    "ensure_role_ready",
    "get_client",
    "get_role_config",
]
