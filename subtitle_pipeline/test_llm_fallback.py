"""字幕 LLM 兜底单测（parse_role fallback / get_role_chain / complete 顺延 / Ollama CLI 兜底）。

不依赖真实 provider：monkeypatch get_role_chain、build_client、subprocess，隔离验证顺延逻辑。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from llm import factory                      # noqa: E402
from llm.config import RoleConfig, parse_role  # noqa: E402


def _cfg(provider, model):
    return RoleConfig(provider=provider, model=model)


class _OkClient:
    def complete(self, prompt, timeout=0):
        return "OK:" + prompt


class _FailClient:
    def complete(self, prompt, timeout=0):
        raise RuntimeError("boom")


def test_parse_role_fallback_and_env_default():
    """parse_role 解析 fallback 列表；无 fallback 时为空；model 回落 fallback_model。"""
    c = parse_role({"provider": "ollama", "model": "a",
                    "fallback": [{"provider": "openai_compat", "model": "b"}]},
                   fallback_model="x")
    assert c.model == "a"
    assert len(c.fallback) == 1 and c.fallback[0].model == "b"
    c2 = parse_role({"provider": "ollama"}, fallback_model="x")
    assert c2.model == "x" and c2.fallback == []


def test_complete_falls_back_to_next(monkeypatch):
    """primary 失败自动顺延 fallback，返回首个成功结果。"""
    monkeypatch.setattr(factory, "get_role_chain",
                        lambda role: [_cfg("ollama", "a"), _cfg("openai_compat", "b")])
    clients = iter([_FailClient(), _OkClient()])
    monkeypatch.setattr(factory, "build_client", lambda cfg: next(clients))
    assert factory.complete("chat", "hi") == "OK:hi"


def test_complete_all_fail_raises(monkeypatch):
    """全部失败才抛错，且抛出最后一次的异常。"""
    monkeypatch.setattr(factory, "get_role_chain",
                        lambda role: [_cfg("ollama", "a"), _cfg("openai_compat", "b")])
    monkeypatch.setattr(factory, "build_client", lambda cfg: _FailClient())
    with pytest.raises(RuntimeError, match="boom"):
        factory.complete("chat", "hi")


def test_complete_empty_chain_raises(monkeypatch):
    """链为空：抛“无可用 provider”错。"""
    monkeypatch.setattr(factory, "get_role_chain", lambda role: [])
    with pytest.raises(RuntimeError, match="no providers"):
        factory.complete("chat", "hi")


def _ollama_client_with_dead_http(monkeypatch, run_result):
    """构造一个 HTTP 必失败、subprocess 被接管、无重试等待的 OllamaClient。"""
    from types import SimpleNamespace

    from llm import ollama_client as oc

    captured = {}

    def _fake_run(argv, **kw):
        captured["argv"] = list(argv)
        captured["kw"] = kw
        return run_result

    def _dead_post(_prompt, _timeout):
        raise RuntimeError("http down")

    monkeypatch.setattr(oc, "subprocess", SimpleNamespace(run=_fake_run))
    monkeypatch.setattr(oc, "time", SimpleNamespace(sleep=lambda _s: None))
    client = oc.OllamaClient("translategemma:4b", ollama_bin="ollama")
    monkeypatch.setattr(client, "_post", _dead_post)
    return client, captured


def test_ollama_cli_fallback_pipes_prompt_via_stdin(monkeypatch):
    """回归：CLI 兜底必须经 stdin(utf-8) 传 prompt，不能塞 argv。

    Windows 上 subprocess 会把 argv 按 GBK 编码，含 GBK 不可编码字符的中文提示词
    会抛 UnicodeEncodeError，导致 translate / summary 的失败重试兜底失效。
    """
    from types import SimpleNamespace

    result = SimpleNamespace(
        returncode=0, stdout="  Okay, let's eat.  ", stderr=""
    )
    client, captured = _ollama_client_with_dead_http(monkeypatch, result)
    prompt = "Translate to English: 好，开饭了。🍚"

    assert client.complete(prompt, timeout=5) == "Okay, let's eat."
    assert captured["argv"] == ["ollama", "run", "translategemma:4b"]
    assert captured["kw"]["input"] == prompt
    assert captured["kw"]["encoding"] == "utf-8"
    assert captured["kw"]["text"] is True


def test_ollama_cli_fallback_raises_on_nonzero(monkeypatch):
    """CLI 兜底返回非 0 时抛出带 stderr（已剥离 ANSI）的 RuntimeError。"""
    from types import SimpleNamespace

    result = SimpleNamespace(
        returncode=1, stdout="", stderr="\x1b[2Kmodel not found"
    )
    client, _ = _ollama_client_with_dead_http(monkeypatch, result)

    with pytest.raises(RuntimeError, match="model not found"):
        client.complete("hi", timeout=5)


def test_console_safe_escapes_non_gbk_chars(monkeypatch):
    """GBK 控制台下保留可编码中文、转义不可编码字符，且不抛 UnicodeEncodeError。"""
    from types import SimpleNamespace

    from llm import factory

    monkeypatch.setattr(
        factory, "sys", SimpleNamespace(stdout=SimpleNamespace(encoding="gbk"))
    )
    safe = factory._console_safe("好，开饭了。🍚")
    assert "好" in safe
    assert "🍚" not in safe
    safe.encode("gbk")


def test_complete_keeps_original_error_when_log_has_non_gbk_text(monkeypatch):
    """回归：provider 异常含非 GBK 字符时，兜底日志不能把异常升级成 UnicodeEncodeError。"""
    from types import SimpleNamespace

    from llm import factory

    monkeypatch.setattr(
        factory, "sys", SimpleNamespace(stdout=SimpleNamespace(encoding="gbk"))
    )

    class _BoomClient:
        def complete(self, prompt, timeout=0):
            raise RuntimeError("模型失败 😋")

    monkeypatch.setattr(factory, "get_role_chain", lambda role: [_cfg("ollama", "m")])
    monkeypatch.setattr(factory, "build_client", lambda cfg: _BoomClient())
    with pytest.raises(RuntimeError, match="模型失败"):
        factory.complete("translate", "hi")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
