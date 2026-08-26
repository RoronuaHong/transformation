from __future__ import annotations

from pathlib import Path

from .anthropic_client import AnthropicCompatClient, TokenHubClient
from .config import RoleConfig, load_raw_config, resolve_profile
from .ollama_client import OllamaClient
from .openai_client import OpenAICompatClient

_profile_name: str = "local"
_roles: dict[str, RoleConfig] = {}
_config_path: Path | None = None
_model_overrides: dict[str, str] = {}


def configure_llm(
    *,
    profile: str | None = None,
    config_path: Path | None = None,
    chat_model: str | None = None,
    translate_model: str | None = None,
) -> str:
    """Load profile into process state. Returns active profile name."""
    global _profile_name, _roles, _config_path, _model_overrides
    _config_path = config_path
    data = load_raw_config(config_path)
    name, roles = resolve_profile(data, profile=profile)
    _profile_name = name
    _roles = roles
    _model_overrides = {}
    if chat_model:
        _model_overrides["chat"] = chat_model
    if translate_model:
        _model_overrides["translate"] = translate_model
    print(
        f"[llm] profile={name} "
        f"chat={get_role_config('chat').provider}:{get_role_config('chat').model} "
        f"translate={get_role_config('translate').provider}:{get_role_config('translate').model}"
    )
    return name


def _ensure_configured() -> None:
    if not _roles:
        configure_llm()


def get_role_config(role: str) -> RoleConfig:
    _ensure_configured()
    if role not in _roles:
        raise KeyError(f"unknown llm role {role!r}; use chat|translate")
    cfg = _roles[role]
    override = _model_overrides.get(role)
    if override and override != cfg.model:
        return RoleConfig(
            provider=cfg.provider,
            model=override,
            base_url=cfg.base_url,
            api_key_env=cfg.api_key_env,
            temperature=cfg.temperature,
            prefer_anthropic=cfg.prefer_anthropic,
        )
    return cfg


def build_client(
    cfg: RoleConfig,
) -> OllamaClient | OpenAICompatClient | AnthropicCompatClient | TokenHubClient:
    provider = cfg.provider
    if provider == "ollama":
        return OllamaClient(
            cfg.model,
            base_url=cfg.resolve_base_url(),
            temperature=cfg.temperature,
        )
    if provider == "openai_compat":
        key = cfg.resolve_api_key()
        if not key:
            raise RuntimeError(
                f"openai_compat role needs env {cfg.api_key_env or 'API_KEY'}"
            )
        base = cfg.resolve_base_url() or "https://api.openai.com/v1"
        return OpenAICompatClient(
            cfg.model, base_url=base, api_key=key, temperature=cfg.temperature
        )
    if provider in ("tokenhub", "tencent", "hy3"):
        key = cfg.resolve_api_key()
        if not key:
            raise RuntimeError(
                "tokenhub role needs env ANTHROPIC_AUTH_TOKEN "
                "(or TOKENHUB_API_KEY / api_key_env)"
            )
        base = cfg.resolve_base_url() or TokenHubClient.DEFAULT_BASE
        return TokenHubClient(
            cfg.model or "hy3",
            base_url=base,
            api_key=key,
            temperature=cfg.temperature,
            prefer_anthropic=cfg.prefer_anthropic,
        )
    if provider in ("anthropic_compat", "anthropic"):
        key = cfg.resolve_api_key()
        if not key:
            raise RuntimeError(
                f"anthropic_compat role needs env {cfg.api_key_env or 'ANTHROPIC_AUTH_TOKEN'}"
            )
        base = cfg.resolve_base_url() or "https://api.anthropic.com"
        # TokenHub host via anthropic_compat → prefer TokenHubClient for better UX
        if "tokenhub" in base.lower() or "tencentmaas" in base.lower():
            return TokenHubClient(
                cfg.model or "hy3",
                base_url=base,
                api_key=key,
                temperature=cfg.temperature,
                prefer_anthropic=True,
            )
        return AnthropicCompatClient(
            cfg.model, base_url=base, api_key=key, temperature=cfg.temperature
        )
    raise ValueError(f"unsupported llm provider: {provider}")


def get_client(role: str, *, model: str | None = None):
    cfg = get_role_config(role)
    if model:
        cfg = RoleConfig(
            provider=cfg.provider,
            model=model,
            base_url=cfg.base_url,
            api_key_env=cfg.api_key_env,
            temperature=cfg.temperature,
            prefer_anthropic=cfg.prefer_anthropic,
        )
    return build_client(cfg)


def ensure_role_ready(role: str, *, model: str | None = None) -> None:
    client = get_client(role, model=model)
    client.ensure_ready()


def complete(
    role: str,
    prompt: str,
    *,
    timeout: int = 300,
    model: str | None = None,
) -> str:
    client = get_client(role, model=model)
    return client.complete(prompt, timeout=timeout)


def active_profile() -> str:
    _ensure_configured()
    return _profile_name
