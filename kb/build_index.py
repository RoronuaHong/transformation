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
# 默认 Milvus Lite（本地文件，无需 Docker）；设 MILVUS_URI=http://host:19530 可切回独立服务
LITE_DB = os.path.join(KB_DIR, "milvus_kb.db")
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

    # 默认 Milvus Lite（本地文件，无需 Docker）；设 MILVUS_URI=http://host:19530 可切回独立服务
    uri = os.environ.get("MILVUS_URI", LITE_DB)
    client = MilvusClient(uri)
    if client.has_collection(COLLECTION):
        # Lite 模式下 drop 很快；统一重建以保证索引干净
        client.drop_collection(COLLECTION)
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
