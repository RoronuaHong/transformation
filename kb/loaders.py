"""读取 sources.yaml，把各类源转成纯文本块用于切片/嵌入。

支持类型：
  - ts-export       : TypeScript export（去类型注解保留内容）
  - markdown        : 单文件 .md / .txt
  - pdf / docx / xlsx : 办公文档（PDF / Word / Excel）
  - dir-markdown    : 目录，递归读 .md/.txt 及办公文档
  - dir-office      : 目录，只读办公文档（PDF/Word/Excel）

办公解析依赖缺失时优雅跳过（打印 [skip]），不会中断整库索引。
"""
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


def _read_pdf(path: str) -> str:
    try:
        import pdfplumber
    except ImportError:
        try:
            from pypdf import PdfReader
            return "\n".join((p.extract_text() or "") for p in PdfReader(path).pages)
        except ImportError:
            print(f"[skip] 需 pdfplumber 或 pypdf 才能读 PDF: {path}")
            return ""
    with pdfplumber.open(path) as pdf:
        return "\n".join((pg.extract_text() or "") for pg in pdf.pages)


def _read_docx(path: str) -> str:
    try:
        import docx
    except ImportError:
        print(f"[skip] 需 python-docx 才能读 DOCX: {path}")
        return ""
    return "\n".join(p.text for p in docx.Document(path).paragraphs)


def _read_xlsx(path: str) -> str:
    try:
        import openpyxl
    except ImportError:
        print(f"[skip] 需 openpyxl 才能读 XLSX: {path}")
        return ""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    out = []
    for ws in wb.worksheets:
        for row in ws.iter_rows(values_only=True):
            cells = [str(c) for c in row if c is not None]
            if cells:
                out.append("\t".join(cells))
    return "\n".join(out)


_OFFICE_EXT = (".pdf", ".docx", ".xlsx")


def _read_any(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        return _read_pdf(path)
    if ext == ".docx":
        return _read_docx(path)
    if ext == ".xlsx":
        return _read_xlsx(path)
    return _read_file(path)


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
        elif stype in ("markdown", "pdf", "docx", "xlsx"):
            docs.append((s["path"], _read_any(path), label))
        elif stype in ("dir-markdown", "dir-office") and os.path.isdir(path):
            for fn in os.listdir(path):
                if fn.lower().endswith((".md", ".txt") + _OFFICE_EXT):
                    full = os.path.join(path, fn)
                    docs.append((os.path.relpath(full, root), _read_any(full), label))
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
