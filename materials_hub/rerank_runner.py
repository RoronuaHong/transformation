# -*- coding: utf-8 -*-
"""bge-reranker-v2-m3 ONNX 重排 runner:由 materials_hub.core 以 **SP venv python**
子进程调用(该 venv 已装 onnxruntime-gpu + tokenizers;hub 自身保持零依赖)。

模型文件:
    materials_hub/models/rerank/model.onnx     (~560MB fp32,onnx-community 官方转换)
    materials_hub/models/rerank/tokenizer.json (XLM-RoBERTa BPE,tokenizers 库直接加载,
                                                无需 sentencepiece)

协议:
    stdin : {"query": "...", "texts": ["...", ...]}
    stdout: {"ok": true, "scores": [float, ...]}   与 texts 等长同序,分数越高越相关
            {"error": "..."}

为什么走 cross-encoder:RRF/稠密余弦是「向量空间里的距离」,而 cross-encoder 把
(query, doc) 拼在一起过模型,能看见词级交互,是检索精度的最后一道精排
(bge-reranker-v2-m3 与库内 bge-m3 同源配套,多语一致)。
"""
from __future__ import annotations

import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

_HUB = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR = os.environ.get("VITUAL_RERANK_DIR", "").strip() or os.path.join(
    _HUB, "models", "rerank")
# XLM-R 上限 512;query+doc 截断到 512 已足够(doc 里 name/描述在前,截断不伤要点)
MAX_LEN = int(os.environ.get("VITUAL_RERANK_MAXLEN", "512") or "512")

_STATE = {}


def _load():
    if _STATE.get("sess") is not None:
        return
    import onnxruntime as ort
    from tokenizers import Tokenizer

    mp = os.path.join(MODEL_DIR, "model.onnx")
    tp = os.path.join(MODEL_DIR, "tokenizer.json")
    if not os.path.isfile(mp) or not os.path.isfile(tp):
        raise FileNotFoundError("model.onnx/tokenizer.json 不在 %s" % MODEL_DIR)
    tok = Tokenizer.from_file(tp)
    tok.enable_truncation(max_length=MAX_LEN)
    pad_id = tok.token_to_id("<pad>")
    tok.enable_padding(pad_id=pad_id if pad_id is not None else 1,
                       pad_token="<pad>", direction="right")
    so = ort.SessionOptions()
    so.intra_op_num_threads = max(1, (os.cpu_count() or 4) - 1)
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    # 默认 CPU:本机实测 CUDA EP 初始化会在 ORT 内部日志触发 GBK UnicodeDecodeError,
    # 且 top-60 精排 CPU 数秒内完成,收益不值得这个雷。要试 CUDA 设
    # VITUAL_RERANK_PROVIDERS="CUDAExecutionProvider,CPUExecutionProvider"。
    providers = [p.strip() for p in os.environ.get(
        "VITUAL_RERANK_PROVIDERS", "CPUExecutionProvider").split(",") if p.strip()]
    sess = ort.InferenceSession(mp, sess_options=so, providers=providers)
    _STATE.update(
        sess=sess, tok=tok,
        ins=[i.name for i in sess.get_inputs()],
        out=sess.get_outputs()[0].name,
    )


def score_pairs(query, texts):
    """返回与 texts 等长的 relevance 分数列表(relevance logit,越高越相关)。"""
    _load()
    import numpy as np

    sess, tok = _STATE["sess"], _STATE["tok"]
    # pair 编码:XLM-R cross-encoder 期望 <s> query </s></s> doc </s>(query 在前)
    encs = tok.encode_batch([(query, t) for t in texts])
    ids = np.asarray([e.ids for e in encs], dtype=np.int64)
    att = np.asarray([e.attention_mask for e in encs], dtype=np.int64)
    feed = {}
    for name in _STATE["ins"]:
        if "input_ids" in name:
            feed[name] = ids
        elif "attention_mask" in name:
            feed[name] = att
        elif "token_type_ids" in name:
            feed[name] = np.zeros_like(ids)
    logits = sess.run([_STATE["out"]], feed)[0]
    return [float(x) for x in np.asarray(logits).reshape(-1)]


def main():
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception as e:
        print(json.dumps({"error": "bad_stdin:%s" % e}, ensure_ascii=False))
        return
    q = (payload.get("query") or "").strip()
    texts = payload.get("texts") or []
    if not q or not texts:
        print(json.dumps({"error": "empty_query_or_texts"}, ensure_ascii=False))
        return
    try:
        scores = score_pairs(q, [str(t) for t in texts])
        print(json.dumps({"ok": True, "scores": scores}, ensure_ascii=False))
    except Exception as e:
        print(json.dumps({"error": "%s: %s" % (type(e).__name__, e)},
                         ensure_ascii=False))


if __name__ == "__main__":
    main()
