from __future__ import annotations

from typing import Protocol


class LLMClient(Protocol):
    provider: str
    model: str

    def complete(self, prompt: str, *, timeout: int = 300) -> str: ...

    def ensure_ready(self) -> None: ...
