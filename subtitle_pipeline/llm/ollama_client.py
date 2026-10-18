from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.error
import urllib.request
from shutil import which

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def _strip_ansi(text: str) -> str:
    """ollama CLI 的进度条/光标控制符会污染错误信息。"""
    return _ANSI_RE.sub("", text or "").replace("\r", "\n").strip()


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

    def _post(self, prompt: str, timeout: int) -> str:
        body = json.dumps(
            {
                "model": self.model,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": self.temperature,
                    # 抑制小模型在长列表任务上的 token 重复退化
                    # （"prediction aborted, token repeat limit reached"）
                    "repeat_penalty": 1.1,
                    # ollama 默认 num_ctx=2048，管线的大提示词（整篇转写/结构化笔记）
                    # 会超限 → 模型返回空 → 静默空笔记。显式开到 16K。
                    "num_ctx": 16384,
                },
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/api/generate",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return (data.get("response") or "").strip()

    def complete(self, prompt: str, *, timeout: int = 300) -> str:
        # HTTP 路径重试一次，避免瞬时错误直接落到慢且噪音大的 CLI 兜底
        try:
            try:
                return self._post(prompt, timeout)
            except Exception:
                time.sleep(2)
                return self._post(prompt, timeout)
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
                raise RuntimeError(_strip_ansi(r.stderr or r.stdout or "ollama run failed"))
            return _strip_ansi(r.stdout)

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
