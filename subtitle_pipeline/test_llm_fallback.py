"""字幕 LLM 多云兜底链单测（parse_role fallback / get_role_chain / complete 顺延）。

不依赖真实 provider：monkeypatch get_role_chain 与 build_client，隔离验证顺延逻辑。
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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
