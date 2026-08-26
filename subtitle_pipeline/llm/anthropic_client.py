from __future__ import annotations

import json
import urllib.error
import urllib.request


def _join_messages_url(base_url: str) -> str:
    """Normalize host → …/v1/messages for Anthropic-compatible gateways."""
    u = (base_url or "").rstrip("/")
    if u.endswith("/messages"):
        return u
    if u.endswith("/v1"):
        return f"{u}/messages"
    return f"{u}/v1/messages"


def _extract_text(data: dict) -> str:
    parts = data.get("content") or []
    texts: list[str] = []
    for p in parts:
        if isinstance(p, dict) and p.get("type") == "text":
            texts.append(str(p.get("text") or ""))
        elif isinstance(p, str):
            texts.append(p)
    if texts:
        return "\n".join(texts).strip()
    choices = data.get("choices") or []
    if choices:
        msg = choices[0].get("message") or {}
        content = msg.get("content")
        if isinstance(content, list):
            return "\n".join(
                str(x.get("text") or "") if isinstance(x, dict) else str(x) for x in content
            ).strip()
        return (content or "").strip()
    return (data.get("completion") or data.get("response") or "").strip()


def _http_error_detail(e: urllib.error.HTTPError) -> str:
    try:
        body = e.read().decode("utf-8", errors="replace")[:500]
    except Exception:
        body = ""
    return f"HTTP {e.code}: {body or e.reason}"


class AnthropicCompatClient:
    """Anthropic Messages API (/v1/messages) — official Anthropic + TokenHub etc."""

    provider = "anthropic_compat"

    def __init__(
        self,
        model: str,
        *,
        base_url: str,
        api_key: str,
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.temperature = temperature
        self.max_tokens = max_tokens

    def complete(self, prompt: str, *, timeout: int = 300) -> str:
        url = _join_messages_url(self.base_url)
        body = json.dumps(
            {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "temperature": self.temperature,
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": prompt}],
                    }
                ],
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "Authorization": f"Bearer {self.api_key}",
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"anthropic_compat {url}: {_http_error_detail(e)}") from e
        return _extract_text(data)

    def ensure_ready(self) -> None:
        if not self.api_key:
            raise RuntimeError(
                "anthropic_compat: missing API key "
                "(set ANTHROPIC_AUTH_TOKEN or api_key_env)"
            )
        if not self.base_url:
            raise RuntimeError("anthropic_compat: missing base_url")
        print(f"[llm] anthropic_compat model={self.model} @ {self.base_url}")


class TokenHubClient:
    """Tencent TokenHub (hy3 etc.) via OpenAI Chat Completions — primary recommended path.

    Also works with the same ANTHROPIC_* env that Claude Code uses:
      ANTHROPIC_BASE_URL=https://tokenhub.tencentmaas.com
      ANTHROPIC_AUTH_TOKEN=...
    """

    provider = "tokenhub"
    DEFAULT_BASE = "https://tokenhub.tencentmaas.com"

    def __init__(
        self,
        model: str = "hy3",
        *,
        base_url: str | None = None,
        api_key: str,
        temperature: float = 0.2,
        prefer_anthropic: bool = False,
        max_tokens: int = 4096,
    ) -> None:
        self.model = model or "hy3"
        raw = (base_url or self.DEFAULT_BASE).rstrip("/")
        # Accept bare host or …/v1
        if raw.endswith("/v1"):
            raw = raw[:-3]
        self.base_url = raw or self.DEFAULT_BASE
        self.api_key = api_key
        self.temperature = temperature
        self.prefer_anthropic = prefer_anthropic
        self.max_tokens = max_tokens

    def complete(self, prompt: str, *, timeout: int = 300) -> str:
        if self.prefer_anthropic:
            return self._complete_anthropic(prompt, timeout=timeout)
        try:
            return self._complete_openai(prompt, timeout=timeout)
        except Exception as first:
            # Fallback: Anthropic Messages protocol (same gateway)
            try:
                return self._complete_anthropic(prompt, timeout=timeout)
            except Exception:
                raise first

    def _complete_openai(self, prompt: str, *, timeout: int) -> str:
        url = f"{self.base_url}/v1/chat/completions"
        body = json.dumps(
            {
                "model": self.model,
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"tokenhub openai {url}: {_http_error_detail(e)}") from e
        return _extract_text(data)

    def _complete_anthropic(self, prompt: str, *, timeout: int) -> str:
        client = AnthropicCompatClient(
            self.model,
            base_url=self.base_url,
            api_key=self.api_key,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )
        return client.complete(prompt, timeout=timeout)

    def ensure_ready(self) -> None:
        if not self.api_key:
            raise RuntimeError(
                "tokenhub: missing API key (set ANTHROPIC_AUTH_TOKEN)"
            )
        mode = "anthropic" if self.prefer_anthropic else "openai(+anthropic fallback)"
        print(f"[llm] tokenhub model={self.model} @ {self.base_url} via {mode}")
