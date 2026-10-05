"""可观测性:结构化日志 + 指标埋点(零依赖、全离线,仅标准库)。

对应《最佳实践》§8 G 维度的两个历史缺口:

1. **结构化日志** —— 原先 server/MCP 只有零星 stderr,不带 request id / 耗时,排障靠猜。
   现在每次请求/工具调用/检索都写一条 JSON 行日志(带 `rid` 请求 id、`ms` 耗时、`ok` 结果),
   落到 ``index/logs/hub-YYYYMMDD.jsonl``(派生数据,可删)。

2. **指标埋点** —— 检索延迟(p50/p95)、零命中率、失败率原先完全未量化。
   现在按 kind(http / mcp / search)累计计数与耗时分位,经
   ``/api/metrics``(HTTP)与 ``python cli.py metrics``(CLI)暴露。

设计取舍(对齐离线/零依赖原则):
  - 不引入 prometheus/client 等任何依赖,纯 stdlib;
  - 日志与指标均为**派生数据**,落 ``index/`` 下,不污染资产;
  - 埋点失败绝不影响主流程(全部 try/except 兜底,可观测性不得反过来拖垮服务)。
"""
import os
import json
import time
import threading
import collections

HUB = os.path.dirname(os.path.abspath(__file__))
# 派生目录:日志可随时删。测试可临时改写 obs.LOG_DIR 做隔离。
LOG_DIR = os.path.join(HUB, "index", "logs")

_DUR_WINDOW = 300          # 每个 kind 保留最近 N 次耗时(算 p50/p95 用)
_LOCK = threading.Lock()
_SEQ = [0]
_METRICS = collections.defaultdict(
    lambda: {"count": 0, "errors": 0, "zero_hits": 0,
             "durations": collections.deque(maxlen=_DUR_WINDOW)})


def new_id():
    """短请求 id(进程内递增 + 时间戳尾),够排障定位即可。"""
    with _LOCK:
        _SEQ[0] += 1
        return "%d-%d" % (int(time.time()) % 100000, _SEQ[0])


def record(kind, *, duration_ms=None, ok=True, zero_hit=False, **fields):
    """记一次事件:更新内存计数器 + 追加一行结构化日志。

    任何异常都被吞掉(可观测性不得影响主流程)。
    """
    try:
        with _LOCK:
            m = _METRICS[kind]
            m["count"] += 1
            if not ok:
                m["errors"] += 1
            if zero_hit:
                m["zero_hits"] += 1
            if duration_ms is not None:
                m["durations"].append(float(duration_ms))
    except Exception:  # noqa: BLE001
        pass
    try:
        _write_log(kind, duration_ms=duration_ms, ok=ok,
                   zero_hit=zero_hit, **fields)
    except Exception:  # noqa: BLE001
        pass


def _write_log(kind, **fields):
    ev = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": kind}
    ev.update(fields)
    d = LOG_DIR
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "hub-%s.jsonl" % time.strftime("%Y%m%d"))
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")


def _pct(vals, p):
    """分位数(p∈[0,1]);空列表返回 0.0。"""
    if not vals:
        return 0.0
    s = sorted(vals)
    i = int(round((len(s) - 1) * p))
    return round(float(s[max(0, min(i, len(s) - 1))]), 2)


def snapshot():
    """指标快照:每 kind 的调用数/失败率/零命中率/耗时分位。"""
    out = {}
    with _LOCK:
        for kind, m in _METRICS.items():
            n = m["count"]
            out[kind] = {
                "count": n,
                "errors": m["errors"],
                "error_rate": round(m["errors"] / n, 4) if n else 0.0,
                "zero_hits": m["zero_hits"],
                "zero_hit_rate": round(m["zero_hits"] / n, 4) if n else 0.0,
                "ms_p50": _pct(m["durations"], 0.50),
                "ms_p95": _pct(m["durations"], 0.95),
                "ms_max": round(float(max(m["durations"])), 2) if m["durations"] else 0.0,
            }
    return out


def reset():
    """仅供测试:清空内存计数器(不动日志文件)。"""
    with _LOCK:
        _METRICS.clear()


def log_files(days=1):
    """最近 days 天的日志文件路径(按日期倒序)。"""
    try:
        if not os.path.isdir(LOG_DIR):
            return []
        names = sorted([n for n in os.listdir(LOG_DIR)
                        if n.startswith("hub-") and n.endswith(".jsonl")],
                       reverse=True)
        return [os.path.join(LOG_DIR, n) for n in names[:max(1, int(days))]]
    except OSError:
        return []


def aggregate_from_logs(days=1):
    """从落盘日志聚合指标(**跨进程可见**)。

    内存 `snapshot()` 只反映当前进程(服务进程内有用),而独立 CLI 进程内存为空 ——
    所以 `python cli.py metrics` 走这里,从日志反算出调用数/失败率/零命中率/耗时分位。
    """
    agg = {}
    for path in log_files(days):
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    kind = ev.get("kind") or "unknown"
                    a = agg.setdefault(kind, {"count": 0, "errors": 0,
                                              "zero_hits": 0, "durations": []})
                    a["count"] += 1
                    if not ev.get("ok", True):
                        a["errors"] += 1
                    if ev.get("zero_hit"):
                        a["zero_hits"] += 1
                    d = ev.get("duration_ms")
                    if isinstance(d, (int, float)):
                        a["durations"].append(float(d))
        except OSError:
            continue
    out = {}
    for kind, a in agg.items():
        n = a["count"]
        out[kind] = {
            "count": n, "errors": a["errors"],
            "error_rate": round(a["errors"] / n, 4) if n else 0.0,
            "zero_hits": a["zero_hits"],
            "zero_hit_rate": round(a["zero_hits"] / n, 4) if n else 0.0,
            "ms_p50": _pct(a["durations"], 0.50),
            "ms_p95": _pct(a["durations"], 0.95),
            "ms_max": round(float(max(a["durations"])), 2) if a["durations"] else 0.0,
        }
    return out


class Timer(object):
    """with 语法的耗时埋点:with obs.Timer("search", mode="lexical"): ..."""

    def __init__(self, kind, **fields):
        self.kind = kind
        self.fields = fields
        self.t0 = time.perf_counter()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        ms = (time.perf_counter() - self.t0) * 1000.0
        record(self.kind, duration_ms=ms, ok=(exc_type is None),
               **self.fields)
        return False
