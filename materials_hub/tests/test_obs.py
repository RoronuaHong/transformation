"""可观测模块 obs 离线测试(§8 G 维度:结构化日志 + 指标埋点)。

覆盖:计数/失败率/零命中率、耗时分位(p50/p95)、Timer(含异常判失败)、日志落盘、reset。
日志目录重定向到临时目录,不污染真实 index/logs。
运行: python tests/test_obs.py
"""
import os
import sys
import json
import shutil
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import obs  # noqa: E402

_ORIG_LOG_DIR = obs.LOG_DIR


def _tmp():
    d = tempfile.mkdtemp()
    obs.LOG_DIR = d
    obs.reset()
    return d


def _clean(d):
    obs.LOG_DIR = _ORIG_LOG_DIR
    obs.reset()
    shutil.rmtree(d, ignore_errors=True)


def test_record_counts_and_rates():
    d = _tmp()
    try:
        obs.record("search", duration_ms=10.0, ok=True, zero_hit=False, mode="lexical")
        obs.record("search", duration_ms=20.0, ok=False, zero_hit=True, mode="lexical")
        obs.record("search", duration_ms=30.0, ok=True, zero_hit=True, mode="auto")
        s = obs.snapshot()["search"]
        assert s["count"] == 3
        assert s["errors"] == 1
        # 比率保留 4 位小数,故用 1e-3 容差
        assert abs(s["error_rate"] - 1 / 3) < 1e-3
        assert s["zero_hits"] == 2
        assert abs(s["zero_hit_rate"] - 2 / 3) < 1e-3
    finally:
        _clean(d)


def test_duration_percentiles():
    d = _tmp()
    try:
        for ms in range(1, 101):          # 1..100ms
            obs.record("http", duration_ms=float(ms))
        s = obs.snapshot()["http"]
        assert s["count"] == 100
        # p50 约 50ms、p95 约 95ms(允许排序取值的 1 个偏移)
        assert 49.0 <= s["ms_p50"] <= 51.0, s
        assert 94.0 <= s["ms_p95"] <= 96.0, s
        assert s["ms_max"] == 100.0
    finally:
        _clean(d)


def test_timer_records_duration_and_failure():
    d = _tmp()
    try:
        import time as _t
        with obs.Timer("mcp", method="tools/call"):
            _t.sleep(0.01)          # 让耗时可测(纯 pass 会低于分位保留精度)
        assert obs.snapshot()["mcp"]["count"] == 1
        assert obs.snapshot()["mcp"]["errors"] == 0
        assert obs.snapshot()["mcp"]["ms_p50"] >= 10.0
        try:
            with obs.Timer("mcp", method="tools/call"):
                raise ValueError("boom")
        except ValueError:
            pass
        s = obs.snapshot()["mcp"]
        assert s["count"] == 2
        assert s["errors"] == 1          # Timer 把异常记为失败
    finally:
        _clean(d)


def test_log_written_as_jsonl():
    d = _tmp()
    try:
        obs.record("http", duration_ms=5.0, ok=True, rid="1-1",
                   method="GET", path="/api/health", status=200)
        files = os.listdir(d)
        assert len(files) == 1, files
        with open(os.path.join(d, files[0]), encoding="utf-8") as f:
            line = json.loads(f.readline())
        assert line["kind"] == "http"
        assert line["rid"] == "1-1"
        assert line["status"] == 200
        assert line["path"] == "/api/health"
    finally:
        _clean(d)


def test_snapshot_empty_and_reset():
    d = _tmp()
    try:
        assert obs.snapshot() == {}
        obs.record("search", duration_ms=1.0)
        assert obs.snapshot()["search"]["count"] == 1
        obs.reset()
        assert obs.snapshot() == {}
    finally:
        _clean(d)


def test_aggregate_from_logs_crosses_processes():
    """独立进程内存为空,必须能从日志反算出指标(否则 cli.py metrics 恒空)。"""
    d = _tmp()
    try:
        obs.record("search", duration_ms=10.0, ok=True, zero_hit=False, mode="lexical")
        obs.record("search", duration_ms=20.0, ok=False, zero_hit=True, mode="lexical")
        obs.record("http", duration_ms=5.0, ok=True, status=200)
        obs.reset()                                  # 模拟"另一个进程":内存已空
        assert obs.snapshot() == {}
        agg = obs.aggregate_from_logs(days=1)
        assert agg["search"]["count"] == 2
        assert agg["search"]["errors"] == 1
        assert agg["search"]["zero_hits"] == 1
        assert agg["http"]["count"] == 1
        assert agg["http"]["ms_p50"] == 5.0
    finally:
        _clean(d)


def test_new_id_unique():
    ids = {obs.new_id() for _ in range(100)}
    assert len(ids) == 100
