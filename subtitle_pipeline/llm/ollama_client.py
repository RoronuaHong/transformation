from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from shutil import which


class OllamaClient:
    provider = "ollama"

    def __init__(
        self,
        model: str,
        *,
        base_url: str | None = None,
        temperature: float = 0.2,
        ollama_bin: str | None = None,
    ) -> None:
        self.model = model
        self.base_url = (base_url or "http://127.0.0.1:11434").rstrip("/")
        self.temperature = temperature
        self.ollama_bin = ollama_bin or which("ollama")

    def complete(self, prompt: str, *, timeout: int = 300) -> str:
        body = json.dumps(
            {
                "model": self.model,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": self.temperature},
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/api/generate",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return (data.get("response") or "").strip()
        except Exception:
            if not self.ollama_bin:
                raise
            r = subprocess.run(
                [self.ollama_bin, "run", self.model, prompt],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
            if r.returncode != 0:
                raise RuntimeError(r.stderr or r.stdout)
            return (r.stdout or "").strip()

    def ensure_ready(self) -> None:
        try:
            with urllib.request.urlopen(f"{self.base_url}/api/tags", timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            raise RuntimeError(
                f"Ollama is not reachable at {self.base_url}. Start it first. ({e})"
            ) from e
        names: set[str] = set()
        for item in data.get("models") or []:
            name = item.get("name") or item.get("model") or ""
            names.add(name)
            names.add(name.split(":")[0])
            names.add(name.split("/")[-1])
        if self.model not in names and self.model.split(":")[0] not in names:
            raise RuntimeError(
                f"Ollama model {self.model!r} not found locally (will not pull).\n"
                f"  Once online: ollama pull {self.model}\n"
                f"  Installed: {sorted(n for n in names if ':' in n) or sorted(names)}"
            )
        print(f"[llm] ollama model={self.model} @ {self.base_url}")
