from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_DIR = Path(__file__).resolve().parents[1]
EXAMPLE_PATH = DEFAULT_DIR / "llm.example.yaml"
CONFIG_PATH = DEFAULT_DIR / "llm.yaml"


@dataclass
class RoleConfig:
    provider: str  # ollama | openai_compat | anthropic_compat | tokenhub
    model: str
    base_url: str | None = None
    api_key_env: str | None = None
    temperature: float = 0.2
    prefer_anthropic: bool = False

    def resolve_api_key(self) -> str | None:
        envs: list[str] = []
        if self.api_key_env:
            envs.append(self.api_key_env)
        # common aliases for TokenHub / Claude Code
        for alt in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "TOKENHUB_API_KEY"):
            if alt not in envs:
                envs.append(alt)
        for name in envs:
            val = os.environ.get(name)
            if val:
                return val
        return None

    def resolve_base_url(self) -> str | None:
        if not self.base_url:
            return None
        expanded = _expand_env(self.base_url).strip()
        return expanded or None


def _expand_env(value: str) -> str:
    """Replace ${VAR} with environment values."""

    def repl(m: re.Match[str]) -> str:
        return os.environ.get(m.group(1), "")

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", repl, value)


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def load_raw_config(path: Path | None = None) -> dict[str, Any]:
    _load_dotenv(DEFAULT_DIR / ".env")
    p = Path(path) if path else None
    if p is None:
        p = CONFIG_PATH if CONFIG_PATH.exists() else EXAMPLE_PATH
    if not p.exists():
        return {"profile": "local", "profiles": _builtin_local()}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not data.get("profiles"):
        data["profiles"] = _builtin_local()
    return data


def _builtin_local() -> dict[str, Any]:
    host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
    return {
        "local": {
            "chat": {
                "provider": "ollama",
                "model": "gemma4:e2b",
                "base_url": host,
            },
            "translate": {
                "provider": "ollama",
                "model": "translategemma:4b",
                "base_url": host,
            },
        }
    }


def parse_role(raw: dict[str, Any] | None, *, fallback_model: str) -> RoleConfig:
    raw = raw or {}
    return RoleConfig(
        provider=str(raw.get("provider") or "ollama").strip().lower(),
        model=str(raw.get("model") or fallback_model),
        base_url=(str(raw["base_url"]) if raw.get("base_url") else None),
        api_key_env=(str(raw["api_key_env"]) if raw.get("api_key_env") else None),
        temperature=float(raw.get("temperature") if raw.get("temperature") is not None else 0.2),
        prefer_anthropic=bool(raw.get("prefer_anthropic") or False),
    )


def resolve_profile(
    data: dict[str, Any],
    *,
    profile: str | None = None,
) -> tuple[str, dict[str, RoleConfig]]:
    name = (
        profile
        or os.environ.get("VITUAL_LLM_PROFILE")
        or str(data.get("profile") or "local")
    ).strip()
    profiles = data.get("profiles") or {}
    if name not in profiles:
        raise KeyError(
            f"unknown llm profile {name!r}; available: {sorted(profiles)}"
        )
    block = profiles[name] or {}
    roles = {
        "chat": parse_role(block.get("chat"), fallback_model="gemma4:e2b"),
        "translate": parse_role(block.get("translate"), fallback_model="translategemma:4b"),
    }
    return name, roles
