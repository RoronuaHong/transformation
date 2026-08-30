"""A 方案：构建 Milvus 向量索引。

用法:
  .venv/Scripts/python.exe build_index.py
"""
import os
import sys

import yaml
import ollama
from pymilvus import MilvusClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KB_DIR = os.path.dirname(os.path.abspath(__file__))
COLLECTION = "deskline_kb_v1"

sys.path.insert(0, KB_DIR)
from loaders import collect_documents, chunk_text  # noqa: E402


def main():
    with open(os.path.join(KB_DIR, "sources.yaml"), "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    embed_cfg = cfg.get("embed", {})
    base_url = embed_cfg.get("base_url", "http://127.0.0.1:11434")
    model = embed_cfg.get("model", "nomic-embed-text")
    dim = embed_cfg.get("dim", 768)

    oc = ollama.Client(host=base_url)

    # 连接本地 Milvus 服务（Docker 启动后）
    uri = os.environ.get("MILVUS_URI", "http://127.0.0.1:19530")
    client = MilvusClient(uri)
    if client.has_collection(COLLECTION):
        # drop 在某些环境下会阻塞，改为清空数据
        client.delete(COLLECTION, filter="id >= 0")
    else:
        client.create_collection(COLLECTION, dimension=dim, metric_type="COSINE")

    docs = collect_documents(ROOT, os.path.join(KB_DIR, "sources.yaml"))
    rows = []
    for doc_id, text, label in docs:
        for ch in chunk_text(text):
            emb = oc.embeddings(model=model, prompt=ch)["embedding"]
            rows.append({"vector": emb, "text": ch, "source": doc_id, "label": label})

    if rows:
        for i, r in enumerate(rows):
            r["id"] = i
        client.insert(COLLECTION, rows)
    print(f"Indexed {len(rows)} chunks from {len(docs)} documents into Milvus '{COLLECTION}'.")


if __name__ == "__main__":
    main()
