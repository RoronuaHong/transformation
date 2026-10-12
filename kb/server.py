"""A 方案：FastAPI 检索+生成服务（向量库：Milvus Lite）。

端点:
  GET  /health
  POST /search  {query, top_k?}            -> 返回检索到的文本块
  POST /ask     {query, top_k?, history?}  -> 检索 + 调 LLM 生成带引用回答

用法:
  .venv/Scripts/python.exe server.py
"""
import os
import sys

# 本机服务（Ollama / Milvus）必须绕过系统代理：
# Windows 开启系统代理后，httpx 会读注册表把 127.0.0.1 也走代理，导致 502。
def _bypass_localhost_proxy() -> None:
    entries = {"localhost", "127.0.0.1", "::1"}
    for var in ("NO_PROXY", "no_proxy"):
        cur = {p.strip() for p in os.environ.get(var, "").split(",") if p.strip()}
        os.environ[var] = ",".join(sorted(entries | cur))


_bypass_localhost_proxy()

import yaml  # noqa: E402
import ollama  # noqa: E402
import requests  # noqa: E402
from pymilvus import MilvusClient  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from pydantic import BaseModel  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KB_DIR = os.path.dirname(os.path.abspath(__file__))
# 默认 Milvus Lite（本地文件，无需 Docker）；设 MILVUS_URI=http://host:19530 可切回独立 Milvus 服务
LITE_DB = os.path.join(KB_DIR, "milvus_kb.db")
MILVUS_URI = os.environ.get("MILVUS_URI", LITE_DB)
COLLECTION = "deskline_kb_v1"

sys.path.insert(0, KB_DIR)
from loaders import collect_documents  # noqa: E402

with open(os.path.join(KB_DIR, "sources.yaml"), "r", encoding="utf-8") as f:
    CFG = yaml.safe_load(f)
EMBED = CFG.get("embed", {})
GEN = CFG.get("generate", {})

_oc = ollama.Client(host=EMBED.get("base_url", "http://127.0.0.1:11434"))
_embed_model = EMBED.get("model", "nomic-embed-text")
_client = MilvusClient(MILVUS_URI)
# Lite 模式下进程重启后集合可能处于 released 状态，search 前必须 load
if _client.has_collection(COLLECTION):
    _client.load_collection(COLLECTION)


def _embed(text: str) -> list[float]:
    return _oc.embeddings(model=_embed_model, prompt=text)["embedding"]


def _retrieve(query: str, top_k: int):
    vec = _embed(query)
    res = _client.search(
        COLLECTION, data=[vec], limit=top_k, output_fields=["text", "source", "label"]
    )
    out = []
    for hit in res[0]:
        out.append({
            "text": hit["entity"]["text"],
            "source": hit["entity"]["source"],
            "label": hit["entity"]["label"],
            "score": hit["distance"],
        })
    return out


def _generate_ollama(query: str, context: list, history: list, model: str = "gemma4:e2b") -> str:
    ctx_txt = "\n\n".join(
        f"[来源 {i+1}] {c['label']}（{c['source']}）\n{c['text']}" for i, c in enumerate(context)
    )
    sys_prompt = (
        "你是一桌 Deskline 的知识助手，基于以下检索到的资料回答用户问题。"
        "只使用资料内容，若资料不足请说明无法回答。回答末尾用『参考：来源N』标注引用。"
    )
    messages = [{"role": "system", "content": sys_prompt}]
    messages += history
    messages.append({"role": "user", "content": f"资料：\n{ctx_txt}\n\n问题：{query}"})
    r = _oc.chat(model=model, messages=messages, options={"temperature": 0.2})
    return r["message"]["content"]


def _generate_hy3(query: str, context: list, history: list) -> str:
    base_url = os.environ.get(GEN.get("base_url_env"), "")
    api_key = os.environ.get(GEN.get("api_key_env"), "")
    if not base_url or not api_key:
        raise HTTPException(500, "缺少生成模型凭证（ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN）")
    ctx_txt = "\n\n".join(
        f"[来源 {i+1}] {c['label']}（{c['source']}）\n{c['text']}" for i, c in enumerate(context)
    )
    sys_prompt = (
        "你是一桌 Deskline 的知识助手，基于以下检索到的资料回答用户问题。"
        "只使用资料内容，若资料不足请说明无法回答。回答末尾用『参考：来源N』标注引用。"
    )
    messages = [{"role": "user", "content": f"{sys_prompt}\n\n资料：\n{ctx_txt}\n\n问题：{query}"}]
    resp = requests.post(
        f"{base_url.rstrip('/')}/v1/messages",
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
        json={"model": GEN.get("model", "hy3"), "max_tokens": 1024, "messages": messages},
        timeout=120,
    )
    resp.raise_for_status()
    return "".join(b.get("text", "") for b in resp.json().get("content", []))


def _generate(query: str, context: list, history: list) -> str:
    # 默认本地 Ollama 生成；若设了 hy3 凭证则走 hy3
    if os.environ.get(GEN.get("base_url_env")) and os.environ.get(GEN.get("api_key_env")):
        try:
            return _generate_hy3(query, context, history)
        except Exception as e:  # 回退本地
            print("hy3 failed, fallback ollama:", e)
    return _generate_ollama(query, context, history)


app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class Query(BaseModel):
    query: str
    top_k: int = 5
    history: list = []


@app.get("/health")
def health():
    return {"status": "ok", "count": _client.get_collection_stats(COLLECTION)["row_count"]}


@app.post("/search")
def search(q: Query):
    return {"results": _retrieve(q.query, q.top_k)}


def _decision_citation_supported(answer: str, sources: list) -> float | None:
    """调 /v1/systemone 的 noul 判定回答是否被检索资料支撑；服务关/失败返回 None。

    可靠性对齐 core.decision_call：云端网关偶发失败做指数退避重试(默认 2 次)，
    4xx(格式/鉴权)不重试；本地失败即真失败。未支撑始终降级为仅返回检索原文。
    """
    url = os.environ.get("VITUAL_DECISION_URL", "").strip().rstrip("/")
    if not url:
        return None
    import json as _json
    import time as _t
    import urllib.request as _u
    import urllib.error as _ue

    ctx = "\n".join(
        "[%d] %s（%s）：%s" % (i + 1, c.get("label", ""), c.get("source", ""), c.get("text", ""))
        for i, c in enumerate(sources)
    )
    state = "回答：%s\n\n检索资料：\n%s" % (answer, ctx)
    body = {
        "state": state,
        "questions": {
            "c": {
                "type": "noul",
                "instructions": "上述回答是否被检索资料支撑？",
                "criteria": {"true": "回答可由资料证实", "false": "回答含资料未支撑内容"},
            }
        },
    }
    is_or = "openrouter.ai" in url
    req_url = url if is_or else url + "/v1/systemone"
    headers = {"Content-Type": "application/json"}
    try:
        _timeout = float(os.environ.get("VITUAL_DECISION_TIMEOUT", "3"))
    except ValueError:
        _timeout = 3.0
    if is_or and _timeout < 20:
        _timeout = 20.0
    if is_or:
        body["model"] = os.environ.get("VITUAL_DECISION_MODEL", "typesafe/jev-1.13")
        key = os.environ.get("OPENROUTER_API_KEY")
        if key:
            headers["Authorization"] = "Bearer " + key
    retries = 2 if is_or else 0
    last_err = None
    for attempt in range(1 + retries):
        t0 = _t.time()
        try:
            req = _u.Request(
                req_url,
                data=_json.dumps(body).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with _u.urlopen(req, timeout=_timeout) as r:
                ans = _json.loads(r.read().decode("utf-8")).get("answers", {}).get("c", {})
            return float(ans.get("noul")) if "noul" in ans else None
        except _ue.HTTPError as e:
            last_err = "HTTP %s" % e.code
            if e.code < 500:                      # 客户端错(格式/鉴权)不重试
                return None
        except Exception as e:                    # 超时/网络抖动 → 重试
            last_err = e.__class__.__name__
        if attempt < retries:
            _t.sleep(min(2 ** attempt, 4))        # 1s, 2s 退避
    return None


@app.post("/ask")
def ask(q: Query):
    ctx = _retrieve(q.query, q.top_k)
    answer = _generate(q.query, ctx, q.history)
    # 引用核查（P1-2，默认关闭）：未支撑则降级为仅返回检索原文，附提示。
    supported = _decision_citation_supported(answer, ctx)
    if supported is not None:
        try:
            thr = float(os.environ.get("VITUAL_DECISION_MIN_CONF", "0.6"))
        except ValueError:
            thr = 0.6
        if supported < thr:
            notice = "⚠️ 回答未被检索资料充分支撑，已降级为仅返回原始资料：\n\n"
            answer = notice + "\n\n".join(
                "[来源 %d] %s（%s）\n%s" % (i + 1, c["label"], c["source"], c["text"])
                for i, c in enumerate(ctx)
            )
    return {
        "answer": answer,
        "sources": [{"source": c["source"], "label": c["label"]} for c in ctx],
        "citation_supported": supported if supported is not None else True,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("KB_PORT", "8910")))
