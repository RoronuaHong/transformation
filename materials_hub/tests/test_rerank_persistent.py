"""常驻重排器回归测试:验证 rerank_runner.py 由一次性子进程改为**行缓冲常驻循环**后,
模型只在首请求加载一次、进程跨请求复用,且排序结果正确。

前置:需 SP venv(onnxruntime + bge-reranker-v2-m3)。不满足时整文件 skip,不污染离线套件。
运行: python tests/test_rerank_persistent.py
"""
import os
import sys
import json
import subprocess
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core  # noqa: E402


def _rerank_available():
    return bool(core.onnx_rerank_available()) and bool(core._clip_python()) \
        and os.path.isfile(os.path.join(core.HUB, "rerank_runner.py"))


def test_rerank_runner_persistent_loop():
    """rerank_runner.py 常驻:连续两条请求都拿到响应,且进程在首请求(模型加载)后仍存活。"""
    if not _rerank_available():
        print("SKIP: rerank venv/model 不可用")
        return
    py = core._clip_python()
    runner = os.path.join(core.HUB, "rerank_runner.py")
    p = subprocess.Popen([py, runner], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1)
    docs = ["一只橘猫在木地板上奔跑", "发动机拆解维修教程", "旺财在公园草地追飞盘"]
    q = "宠物狗户外活动"
    req = json.dumps({"query": q, "texts": docs}, ensure_ascii=False)
    try:
        p.stdin.write(req + "\n"); p.stdin.flush()
        r1 = json.loads(p.stdout.readline().strip())
        # 第二条请求:进程必须复用(不能每次重启重新加载 560MB 模型)
        p.stdin.write(req + "\n"); p.stdin.flush()
        r2 = json.loads(p.stdout.readline().strip())
        assert p.poll() is None, "rerank 进程在第二条请求后应仍存活(常驻复用)"
        assert r1.get("ok") and isinstance(r1.get("scores"), list), r1
        assert r2.get("ok") and len(r2["scores"]) == len(docs), r2
        # 排序正确性:狗活动应把「旺财追飞盘」排最前
        order = sorted(range(len(docs)), key=lambda i: -r1["scores"][i])
        assert docs[order[0]].startswith("旺财"), order
    finally:
        try:
            p.stdin.close()
        except Exception:
            pass
        try:
            p.kill()
        except Exception:
            pass


def test_onnx_rerank_reuses_process():
    """core._onnx_rerank 两次调用复用同一常驻子进程(模型只加载一次)。"""
    if not _rerank_available():
        print("SKIP: rerank venv/model 不可用")
        return
    core._kill_rerank_proc()  # 确保从干净状态测量
    docs = ["蓝莓松饼烘焙步骤", "旺财在公园草地追飞盘", "发动机拆解维修教程"]
    q = "宠物狗户外活动"
    t0 = time.time()
    idx1 = core._onnx_rerank(q, docs)
    first = time.time() - t0
    proc_after_first = core._RERANK_PROC
    assert proc_after_first is not None and proc_after_first.poll() is None
    t1 = time.time()
    idx2 = core._onnx_rerank(q, docs)
    second = time.time() - t1
    # 复用的第二次应明显快于首查(首查含 560MB 模型加载)
    assert idx1 is not None and idx1 == idx2, (idx1, idx2)
    assert docs[idx1[0]].startswith("旺财"), idx1
    assert second < first, "第二次重排应复用常驻进程,远快于首查(实测 %.1fs vs %.1fs)" % (second, first)
    # 进程身份一致(同一 PID)
    assert core._RERANK_PROC is proc_after_first
    core._kill_rerank_proc()


if __name__ == "__main__":
    test_rerank_runner_persistent_loop()
    test_onnx_rerank_reuses_process()
    print("OK")
