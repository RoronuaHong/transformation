"""读取 sources.yaml，把各类源转成纯文本块用于切片/嵌入。"""
import os
import re
import yaml


def _strip_ts_export(text: str) -> str:
    """把 TS 的 export const/type 转成可读文本（去类型注解、保留内容）。"""
    text = re.sub(r"export\s+type\s+\w+\s*=\{.*?\}\s*", "", text, flags=re.S)
    text = re.sub(r":\s*(string|number|boolean|string\[\])\b", "", text)
    text = re.sub(r"export\s+const\s+\w+\s*=\s*", "", text)
    text = text.replace("as const", "").replace("'use strict';", "")
    return text


def _read_file(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def collect_documents(root: str, sources_yaml: str):
    """返回 [(doc_id, text, label), ...]"""
    with open(sources_yaml, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    docs = []
    for s in cfg.get("sources", []):
        path = os.path.join(root, s["path"])
        label = s.get("label", "")
        stype = s.get("type", "markdown")
        # 知识源可能未纳入版本控制（如 findings.md），缺失时跳过而非中断
        if not os.path.exists(path):
            print(f"[skip] 知识源不存在: {s['path']}")
            continue
        if stype == "ts-export":
            docs.append((s["path"], _strip_ts_export(_read_file(path)), label))
        elif stype == "markdown":
            docs.append((s["path"], _read_file(path), label))
        elif stype == "dir-markdown" and os.path.isdir(path):
            for fn in os.listdir(path):
                if fn.lower().endswith((".md", ".txt")):
                    full = os.path.join(path, fn)
                    docs.append((os.path.relpath(full, root), _read_file(full), label))
    return docs


def chunk_text(text: str, size: int = 800, overlap: int = 120):
    """按字符窗口切片，保留重叠。"""
    text = text.strip()
    if not text:
        return []
    chunks = []
    start = 0
    while start < len(text):
        end = start + size
        chunks.append(text[start:end])
        if end >= len(text):
            break
        start = end - overlap
    return chunks
