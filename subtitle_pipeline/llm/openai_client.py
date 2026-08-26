from __future__ import annotations

import json
import urllib.request


class OpenAICompatClient:
    """OpenAI-compatible chat completions (/v1/chat/completions)."""

    provider = "openai_compat"

    def __init__(
        self,
        model: str,
        *,
        base_url: str,
        api_key: str,
        temperature: float = 0.2,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.temperature = temperature

    def complete(self, prompt: str, *, timeout: int = 300) -> str:
        url = self.base_url
        if not url.endswith("/chat/completions"):
            url = f"{url}/chat/completions"
        body = json.dumps(
            {
                "model": self.model,
                "temperature": self.temperature,
                "messages": [{"role": "user", "content": prompt}],
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
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        choices = data.get("choices") or []
        if not choices:
            return ""
        msg = choices[0].get("message") or {}
        return (msg.get("content") or "").strip()

    def ensure_ready(self) -> None:
        if not self.api_key:
            raise RuntimeError(
                "openai_compat: missing API key (set api_key_env in llm.yaml / env)"
            )
        if not self.base_url:
            raise RuntimeError("openai_compat: missing base_url")
        print(f"[llm] openai_compat model={self.model} @ {self.base_url}")
