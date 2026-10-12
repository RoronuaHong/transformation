"""决策客户端 decision_client 的单元测试：统一入口、重试/超时、截断、解析。

不依赖真实网络/决策服务,用 unittest.mock 替换 urllib.request.urlopen。
"""
import os
import json
import time
from unittest import mock
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import decision_client


def _fake_resp(payload, status=200):
    resp = mock.MagicMock()
    resp.read.return_value = json.dumps(payload).encode("utf-8")
    resp.__enter__.return_value = resp  # 作为 context manager 时 `as r` 返回自身
    resp.__exit__.return_value = False
    return resp


def _set(env):
    os.environ["VITUAL_DECISION_URL"] = env


def test_local_endpoint_no_retry_on_failure():
    """本地服务失败即真失败,不重试(避免无效重试拖慢)。"""
    _set("http://127.0.0.1:8911")
    with mock.patch("decision_client._u.urlopen", side_effect=RuntimeError("boom")) as m:
        assert decision_client.decision_call({"state": "x", "questions": {}}) is None
        assert m.call_count == 1  # 本地不重试


def test_cloud_4xx_no_retry():
    """云端 4xx(格式/鉴权)不重试,直接降级。"""
    _set("https://openrouter.ai/api/alpha/decisions")
    err = decision_client._ue.HTTPError("u", 400, "bad", None, None)
    with mock.patch("decision_client._u.urlopen", side_effect=err) as m:
        assert decision_client.decision_call({"state": "x", "questions": {}}) is None
        assert m.call_count == 1


def test_cloud_5xx_retries_then_succeeds():
    """云端 5xx 指数退避重试(默认 2 次),最终成功返回 answers。"""
    _set("https://openrouter.ai/api/alpha/decisions")
    err = decision_client._ue.HTTPError("u", 503, "x", None, None)
    good = _fake_resp({"answers": {"c": {"noul": 0.9}}})
    with mock.patch("decision_client._u.urlopen", side_effect=[err, err, good]) as m:
        ans = decision_client.decision_call({"state": "x", "questions": {"c": {}}})
    assert ans == {"c": {"noul": 0.9}}
    assert m.call_count == 3  # 初始 + 2 次重试


def test_cloud_timeout_forced_20s():
    """云端即使调用方给小 timeout,底层强制 20s 下限,防假失败降级。"""
    _set("https://openrouter.ai/api/alpha/decisions")
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["timeout"] = timeout
        return _fake_resp({"answers": {"r": {"score": 1}}})

    with mock.patch("decision_client._u.urlopen", side_effect=fake_urlopen):
        decision_client.decision_call({"state": "x", "questions": {"r": {}}}, timeout=3)
    assert captured["timeout"] == 20.0


def test_truncate_state():
    long = "a" * 5000
    assert decision_client.truncate_state(long, max_chars=100).endswith("...(已截断)")
    assert decision_client.truncate_state("short", max_chars=100) == "short"
    # 默认上限 2000
    assert len(decision_client.truncate_state("b" * 5000)) <= 2100


def test_recorder_called():
    """埋点回调在成功/失败路径都被调用(可观测性)。"""
    _set("http://127.0.0.1:8911")
    calls = []
    with mock.patch("decision_client._u.urlopen", side_effect=RuntimeError("x")):
        decision_client.decision_call({"state": "x", "questions": {}},
                                     recorder=lambda a, d, ok, e: calls.append((ok, e)))
    assert calls and calls[0][0] is False


def test_batch_noul_truncates_state():
    """P1-1 批量仲裁同样需截断 state，防长素材描述/多标签腐烂（对齐 Jev 长上下文禁忌）。"""
    import core
    long = "素材描述:" + "y" * 5000
    cap = {}
    with mock.patch.object(
        core, "decision_call",
        side_effect=lambda b, **k: (cap.update(b), {"a0": {"noul": 0.9}})[1]
    ):
        res = core.decision_batch_noul({"a0": ("是否匹配标签X?", "相符", "不符")}, long)
    assert res == {"a0": 0.9}
    assert len(cap["state"]) <= 2100


import core  # noqa: E402


def test_hot_intent_default_is_rule(monkeypatch):
    """默认纯规则：不设开关时 _hot_intent 等同 _query_intent，绝不调 Jev。"""
    monkeypatch.delenv("VITUAL_DECISION_INTENT_HOT", raising=False)

    def _boom(*a, **k):
        raise AssertionError("默认不应调用 Jev")

    monkeypatch.setattr(core, "decision_choice_intent", _boom)
    assert core._hot_intent("id:abc") == "id"
    assert core._hot_intent("这句台词") == "speech"
    assert core._hot_intent("找个做菜的视频") == "text"


def test_hot_intent_enabled_uses_jev(monkeypatch):
    """VITUAL_DECISION_INTENT_HOT=1 时热路径启用 Jev 兜底（离线/实验）。"""
    monkeypatch.setenv("VITUAL_DECISION_INTENT_HOT", "1")
    monkeypatch.setattr(core, "decision_endpoint", lambda: "http://x")
    monkeypatch.setattr(core, "decision_choice_intent",
                        lambda q, criteria=None: ("speech", 0.9, 0.9))
    assert core._hot_intent("某句模糊对白") == "speech"


def test_biz_criteria_and_choice(monkeypatch):
    """业务意图 criteria 独立，可传入 decision_choice_intent。"""
    assert set(core._DECISION_BIZ_CRITERIA) >= {"query", "ingest", "update", "delete", "other"}
    monkeypatch.setattr(core, "decision_endpoint", lambda: "http://x")
    monkeypatch.setattr(core, "decision_call",
                        lambda body, **k: {"intent": {"choice": "delete",
                                                      "probabilities": {"delete": 0.9},
                                                      "confidence": 0.9}})
    got = core.decision_choice_intent("删除所有素材", criteria=core._DECISION_BIZ_CRITERIA)
    assert got == ("delete", 0.9, 0.9)


def test_decision_stats_aggregates(monkeypatch):
    """_record_decision 累计成功率/延迟，decision_stats 汇总。"""
    core._decision_stats_reset()
    monkeypatch.setattr(core.obs, "record", lambda *a, **k: None)
    core._record_decision(False, 0.01, True, None)
    core._record_decision(True, 0.03, False, "HTTP 500")
    s = core.decision_stats()
    assert s["calls"] == 2 and s["ok"] == 1 and s["err"] == 1
    assert s["ok_rate"] == 0.5
    assert s["avg_ms"] == 20.0
    core._decision_stats_reset()


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
