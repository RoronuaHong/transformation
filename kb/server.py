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
COLLECTION = "deskline_kb_v1"

sys.path.insert(0, KB_DIR)
from loaders import collect_documents  # noqa: E402

with open(os.path.join(KB_DIR, "sources.yaml"), "r", encoding="utf-8") as f:
    CFG = yaml.safe_load(f)
EMBED = CFG.get("embed", {})
GEN = CFG.get("generate", {})

_oc = ollama.Client(host=EMBED.get("base_url", "http://127.0.0.1:11434"))
_embed_model = EMBED.get("model", "nomic-embed-text")
_client = MilvusClient(os.environ.get("MILVUS_URI", "http://127.0.0.1:19530"))


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


@app.post("/ask")
def ask(q: Query):
    ctx = _retrieve(q.query, q.top_k)
    answer = _generate(q.query, ctx, q.history)
    return {"answer": answer, "sources": [{"source": c["source"], "label": c["label"]} for c in ctx]}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("KB_PORT", "8910")))
