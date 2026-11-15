# -*- coding: utf-8 -*-
"""素材中心「内容预览」解析层(只读 / 零依赖 / 有界读取)。

对齐 2026 年 OWASP File Upload Cheat Sheet、ASVS 5.0 V3.2.1 与 MDN:

* 前端只拿到 JSON 文本,用 React text node 渲染;不可信 HTML/SVG/JS 一律只展示源码,
  绝不进 DOM 执行,也不作为可执行的 MIME 返回。
* 所有读取都有硬上限(字符 / 行 / 单元格 / ZIP 成员 / 解压字节),防止大文件、
  ZIP 炸弹、XML 实体展开把本机服务拖垮。
* 文本编码按 BOM → 严格 UTF-8 → GB18030 → Big5 顺序探测,中文 Windows 记事本
  生成的 .txt/.srt 不会乱码;分页按「字符」而非字节,切页不会切断多字节字符。
* 浏览器无法原生渲染的格式一律给出明确的降级原因 + 文件头十六进制视图,
  不再只显示 ".XXX" 占位。

扩展名清单与 core.KINDS / DOC_TYPE_BY_EXT 对齐;新增格式只需改本文件一处。
"""
from __future__ import annotations

import csv
import html
import io
import json
import mimetypes
import os
import re
import urllib.parse
import zipfile

MAX_CHARS = 65536           # 单次文本预览字符上限(约 64K,DOM 不会爆)
MAX_OFFSET = 2_000_000      # 分页累计偏移上限
MAX_SCAN_BYTES = 262_144    # JSON / CSV / RTF / 单个 XML 成员的扫描上限
MAX_ZIP_BYTES = 8 * 1024 * 1024
MAX_ZIP_PARTS = 200
MAX_TABLE_ROWS = 120
MAX_TABLE_COLS = 30
MAX_CELL_CHARS = 200
HEX_BYTES = 512

SUBTITLE_EXTS = {"srt", "vtt", "ass", "ssa", "sbv", "sub", "smi", "lrc"}
TEXT_EXTS = {
    "txt", "text", "md", "markdown", "rst", "tex", "log", "out", "err",
    "readme", "obj", "ply", "fcpxml",
}
DATA_TEXT_EXTS = {
    "json", "jsonl", "ndjson", "yaml", "yml", "toml", "ini", "conf",
    "cfg", "env", "properties",
}
CODE_EXTS = {
    "js", "mjs", "cjs", "jsx", "ts", "tsx", "py", "pyw", "sh", "bash",
    "ps1", "bat", "cmd", "css", "scss", "less", "html", "htm", "xhtml",
    "xml", "sql", "php", "rb", "go", "rs", "java", "c", "cpp", "h",
    "hpp", "cs", "swift", "kt", "vue", "svelte",
}
TABLE_TEXT_EXTS = {"csv", "tsv", "tab"}
ZIP_TABLE_EXTS = {"xlsx", "xlsm", "ods"}
ZIP_SECTION_EXTS = {"docx", "docm", "odt", "pptx", "pptm", "odp", "epub"}
LEGACY_OFFICE_EXTS = {"doc", "xls", "ppt", "dot", "xlt", "pot"}
PDF_EXTS = {"pdf"}
IMAGE_NATIVE_EXTS = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "avif", "ico", "svg"}
IMAGE_OTHER_EXTS = {
    "tif", "tiff", "heic", "heif", "raw", "cr2", "cr3", "nef", "arw",
    "dng", "orf", "rw2", "pef", "srw", "x3f",
}
VIDEO_NATIVE_EXTS = {"mp4", "m4v", "webm", "ogv", "mov"}
VIDEO_OTHER_EXTS = {"mkv", "avi", "flv", "ts", "m2ts", "wmv", "mpg", "mpeg", "3gp"}
AUDIO_NATIVE_EXTS = {"mp3", "wav", "ogg", "oga", "m4a", "aac", "flac", "opus"}
FONT_EXTS = {"ttf", "otf", "woff", "woff2", "eot"}

# 可携带脚本/外部实体的格式:直接打开时强制 text/plain + attachment + nosniff。
# SVG 例外:仅当 Sec-Fetch-Dest: image(即 <img> 子资源上下文)才以静态图片返回,
# 顶层导航打开仍是纯文本下载,避免同源存储型 XSS(ASVS 5.0 V3.2.1)。
ACTIVE_EXTS = {
    "html", "htm", "xhtml", "mhtml", "xml", "svg", "svgz",
    "js", "mjs", "cjs", "jsx", "ts", "tsx", "php",
}

MIME_BY_EXT = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp",
    "avif": "image/avif", "ico": "image/x-icon", "svg": "image/svg+xml",
    "tif": "image/tiff", "tiff": "image/tiff", "heic": "image/heic",
    "heif": "image/heif",
    "mp4": "video/mp4", "m4v": "video/mp4", "webm": "video/webm",
    "ogv": "video/ogg", "mov": "video/quicktime", "mkv": "video/x-matroska",
    "avi": "video/x-msvideo", "flv": "video/x-flv", "ts": "video/mp2t",
    "mp3": "audio/mpeg", "wav": "audio/wav", "ogg": "audio/ogg",
    "oga": "audio/ogg", "m4a": "audio/mp4", "aac": "audio/aac",
    "flac": "audio/flac", "opus": "audio/ogg",
    "ttf": "font/ttf", "otf": "font/otf", "woff": "font/woff",
    "woff2": "font/woff2", "eot": "application/vnd.ms-fontobject",
    "pdf": "application/pdf",
    "txt": "text/plain", "text": "text/plain", "md": "text/markdown",
    "markdown": "text/markdown", "log": "text/plain", "out": "text/plain",
    "err": "text/plain", "rst": "text/plain", "tex": "text/plain",
    "json": "application/json", "jsonl": "application/x-ndjson",
    "ndjson": "application/x-ndjson", "yaml": "text/yaml",
    "yml": "text/yaml", "toml": "text/plain", "ini": "text/plain",
    "conf": "text/plain", "cfg": "text/plain", "env": "text/plain",
    "properties": "text/plain", "csv": "text/csv",
    "tsv": "text/tab-separated-values", "tab": "text/tab-separated-values",
    "srt": "text/plain", "vtt": "text/vtt", "ass": "text/plain",
    "ssa": "text/plain", "sbv": "text/plain", "sub": "text/plain",
    "smi": "text/plain", "lrc": "text/plain", "rtf": "application/rtf",
    "html": "text/plain", "htm": "text/plain", "xhtml": "text/plain",
    "xml": "text/plain", "js": "text/plain", "mjs": "text/plain",
    "cjs": "text/plain", "jsx": "text/plain", "ts": "text/plain",
    "tsx": "text/plain", "py": "text/plain", "sh": "text/plain",
    "css": "text/plain", "sql": "text/plain", "obj": "text/plain",
    "ply": "text/plain", "fcpxml": "text/plain",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "epub": "application/epub+zip", "doc": "application/msword",
    "xls": "application/vnd.ms-excel", "ppt": "application/vnd.ms-powerpoint",
    "glb": "model/gltf-binary", "gltf": "model/gltf+json",
}

# 允许 inline(浏览器内直接显示)的类型;其它一律 attachment。
INLINE_EXTS = (
    IMAGE_NATIVE_EXTS | VIDEO_NATIVE_EXTS | VIDEO_OTHER_EXTS
    | AUDIO_NATIVE_EXTS | FONT_EXTS | PDF_EXTS | TEXT_EXTS
    | SUBTITLE_EXTS | TABLE_TEXT_EXTS | DATA_TEXT_EXTS
)


def norm_ext(value: str) -> str:
    """'.TXT' / 'note.TXT' / 'txt' → 'txt'。"""
    v = (value or "").strip().lower()
    if "/" in v or "\\" in v:
        v = os.path.splitext(v)[1]
    elif "." in v and not v.startswith("."):
        # 'note.TXT' 这类带名扩展名也取最后一段；'.TXT'/'txt' 原样走 lstrip 兜底
        v = os.path.splitext(v)[1]
    return v.lstrip(".")


def mime_for(ext: str) -> str:
    e = norm_ext(ext)
    return MIME_BY_EXT.get(e) or mimetypes.guess_type("x." + e)[0] or "application/octet-stream"


def detect_encoding(head: bytes) -> str:
    """BOM → UTF-8 → GB18030 → Big5 → cp1252;返回可直接给 open() 的编码名。"""
    if head.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if head.startswith(b"\xff\xfe\x00\x00") or head.startswith(b"\x00\x00\xfe\xff"):
        return "utf-32"
    if head.startswith(b"\xff\xfe") or head.startswith(b"\xfe\xff"):
        return "utf-16"
    sample = head[:4096]
    try:
        sample.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        pass
    for enc in ("gb18030", "big5"):
        try:
            decoded = sample.decode(enc)
            # 随机二进制也能被 gb18030 解码,要求至少有一定比例的可读字符
            good = sum(1 for ch in decoded if ch.isprintable() or ch in "\r\n\t")
            if decoded and good / len(decoded) > 0.7:
                return enc
        except UnicodeDecodeError:
            continue
    return "cp1252"


def charset_label(enc: str) -> str:
    return {
        "utf-8-sig": "utf-8", "utf-8": "utf-8", "utf-16": "utf-16",
        "utf-32": "utf-32", "gb18030": "gb18030", "big5": "big5",
        "cp1252": "windows-1252",
    }.get(enc, "utf-8")


def looks_text(sample: bytes) -> bool:
    """未知扩展名的内容嗅探:无 NUL 且可打印字节占比高才当文本。"""
    if not sample:
        return False
    if b"\x00" in sample:
        return False
    good = sum(1 for b in sample if 32 <= b < 127 or b in (9, 10, 13) or b >= 128)
    return good / len(sample) > 0.85


def mode_for(ext: str, kind: str = "", sample: bytes = b"") -> tuple[str, bool, str]:
    """返回 (mode, native_supported, reason_code)。前端只按 mode 渲染,不重复判格式。"""
    e = norm_ext(ext)
    if e in SUBTITLE_EXTS:
        return "subtitle", True, ""
    if e in PDF_EXTS:
        return "pdf", True, ""
    if e in IMAGE_NATIVE_EXTS:
        return "image", True, "svg_static" if e == "svg" else ""
    if e in IMAGE_OTHER_EXTS:
        return "image", False, "browser_cannot_decode_image"
    if e in VIDEO_NATIVE_EXTS:
        return "video", True, ""
    if e in VIDEO_OTHER_EXTS:
        return "video", False, "unsupported_container"
    if e in AUDIO_NATIVE_EXTS:
        return "audio", True, ""
    if e in FONT_EXTS:
        return "font", True, ""
    if e in TABLE_TEXT_EXTS:
        return "table", True, ""
    if e in ZIP_TABLE_EXTS:
        return "table", True, "office_text_only"
    if e in ZIP_SECTION_EXTS:
        return "sections", True, "office_text_only"
    if e in LEGACY_OFFICE_EXTS:
        return "hex", False, "legacy_office"
    if e == "rtf":
        return "text", True, "rtf_approximate"
    if e in CODE_EXTS:
        return "code", True, "active_source_only" if e in ACTIVE_EXTS else ""
    if e in TEXT_EXTS or e in DATA_TEXT_EXTS:
        return "text", True, ""
    if looks_text(sample):
        return "text", True, "sniffed_text"
    # 扩展名不认识但 kind 已知:仍给播放器一次机会(渐进增强),失败有 onError 兜底
    if kind == "images":
        return "image", False, "browser_cannot_decode_image"
    if kind == "videos":
        return "video", False, "unsupported_container"
    if kind == "audio":
        return "audio", False, "unsupported_codec"
    return "hex", False, "no_renderer"


def hex_dump(data: bytes) -> str:
    lines = []
    for i in range(0, len(data), 16):
        chunk = data[i:i + 16]
        hx = " ".join(f"{b:02x}" for b in chunk).ljust(47)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{i:08x}  {hx}  |{asc}|")
    return "\n".join(lines)


def _read_text(fp: str, enc: str, offset: int, limit: int) -> tuple[str, int | None, bool]:
    """按字符偏移读取,返回 (文本, 下一偏移或 None, 是否还有更多)。"""
    with open(fp, "r", encoding=enc, errors="replace", newline="") as f:
        if offset:
            f.read(offset)
        chunk = f.read(limit + 1)
        more = len(chunk) > limit
        text = chunk[:limit]
        return text, (offset + len(text) if more else None), more


def _scan_text(fp: str, enc: str) -> tuple[str, bool]:
    """结构化格式(CSV/JSON/RTF)读取:上限 MAX_SCAN_BYTES,不整文件进内存。"""
    with open(fp, "rb") as f:
        raw = f.read(MAX_SCAN_BYTES)
    size = os.path.getsize(fp)
    return raw.decode(enc, errors="replace"), size > MAX_SCAN_BYTES


def _pretty_json(raw: str, truncated: bool) -> tuple[str, bool]:
    try:
        obj = json.loads(raw)
        text = json.dumps(obj, ensure_ascii=False, indent=2)
    except (ValueError, TypeError):
        text = raw
    if len(text) > MAX_CHARS:
        return text[:MAX_CHARS], True
    return text, truncated


def _csv_rows(fp: str, enc: str, ext: str) -> tuple[list[list[str]], bool]:
    raw, truncated = _scan_text(fp, enc)
    # 只有被扫描上限截断时才丢掉半行;小文件最后一行不能丢
    if truncated and "\n" in raw:
        raw = raw.rsplit("\n", 1)[0] + "\n"
    delimiter = "\t" if norm_ext(ext) in ("tsv", "tab") else ","
    rows: list[list[str]] = []
    for i, row in enumerate(csv.reader(io.StringIO(raw), delimiter=delimiter)):
        if i >= MAX_TABLE_ROWS:
            truncated = True
            break
        rows.append([cell[:MAX_CELL_CHARS] for cell in row[:MAX_TABLE_COLS]])
    return rows, truncated


def _zip_parts(fp: str, suffixes: tuple[str, ...]):
    """安全遍历 OOXML/EPUB 文本成员:限制成员数、解压体积,拒绝 XML 实体声明。
    不做 XML 解析(避免 XXE / billion laughs),只做正则文本抽取。"""
    try:
        with zipfile.ZipFile(fp) as z:
            infos = z.infolist()
            if len(infos) > MAX_ZIP_PARTS * 20:
                return
            total = 0
            for info in infos:
                name = info.filename
                if not name.lower().endswith(suffixes):
                    continue
                if info.file_size <= 0 or info.file_size > MAX_SCAN_BYTES:
                    continue
                total += info.file_size
                if total > MAX_ZIP_BYTES:
                    break
                data = z.read(info)
                if b"<!ENTITY" in data.upper():
                    continue
                yield name, data.decode("utf-8", errors="replace")
    except (zipfile.BadZipFile, OSError, RuntimeError, ValueError):
        return


def _xml_text(fragment: str, tag: str) -> str:
    return html.unescape("".join(
        re.sub(r"<[^>]+>", "", t)
        for t in re.findall(r"<" + tag + r"[^>]*>(.*?)</" + tag + r">", fragment, re.S)
    )).strip()


def _docx_sections(fp: str) -> list[dict]:
    for _, xml in _zip_parts(fp, ("word/document.xml",)):
        lines = [_xml_text(p, "w:t") for p in re.split(r"</w:p>", xml)]
        body = "\n".join(x for x in lines if x)
        return [{"title": "1", "text": body[:MAX_CHARS]}]
    return []


def _pptx_sections(fp: str) -> list[dict]:
    parts = [p for p in _zip_parts(fp, (".xml",)) if "/slides/slide" in p[0].lower()]
    parts.sort(key=lambda p: int(re.search(r"slide(\d+)", p[0].lower()).group(1))
               if re.search(r"slide(\d+)", p[0].lower()) else 0)
    out, used = [], 0
    for name, xml in parts[:MAX_ZIP_PARTS]:
        # 逐条抽取所有 <a:t>,保留换行分隔
        texts = [html.unescape(re.sub(r"<[^>]+>", "", t)).strip()
                 for t in re.findall(r"<a:t[^>]*>(.*?)</a:t>", xml, re.S)]
        text = "\n".join(t for t in texts if t)
        if not text:
            continue
        num = re.search(r"slide(\d+)", name.lower())
        take = text[:max(0, MAX_CHARS - used)]
        out.append({"title": num.group(1) if num else name.rsplit("/", 1)[-1], "text": take})
        used += len(take)
        if used >= MAX_CHARS:
            break
    return out


def _epub_sections(fp: str) -> list[dict]:
    out, used = [], 0
    for name, raw in list(_zip_parts(fp, (".xhtml", ".html", ".htm")))[:MAX_ZIP_PARTS]:
        clean = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
        text = re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ",
                      html.unescape(re.sub(r"<[^>]+>", " ", clean)))).strip()
        if not text:
            continue
        take = text[:max(0, MAX_CHARS - used)]
        out.append({"title": name.rsplit("/", 1)[-1], "text": take})
        used += len(take)
        if used >= MAX_CHARS:
            break
    return out


def _xlsx_rows(fp: str) -> list[list[str]]:
    shared: list[str] = []
    for _, xml in _zip_parts(fp, ("xl/sharedstrings.xml",)):
        for si in re.findall(r"<si>(.*?)</si>", xml, re.S):
            shared.append("".join(html.unescape(re.sub(r"<[^>]+>", "", t))
                                  for t in re.findall(r"<t[^>]*>(.*?)</t>", si, re.S)))
        break
    sheets = list(_zip_parts(fp, ("xl/worksheets/sheet1.xml",)))
    if not sheets:
        sheets = [p for p in _zip_parts(fp, (".xml",)) if "<sheetData" in p[1]]
    for _, xml in sheets:
        if "<sheetData" not in xml:
            continue
        rows: list[list[str]] = []
        for row_xml in re.findall(r"<row[^>]*>(.*?)</row>", xml, re.S)[:MAX_TABLE_ROWS]:
            cells: dict[int, str] = {}
            for m in re.finditer(r"<c\b([^>]*?)(?:/>|>(.*?)</c>)", row_xml, re.S):
                attrs, body = m.group(1), m.group(2) or ""
                ref = re.search(r'\br="([A-Z]+)', attrs)
                col = 0
                if ref:
                    for ch in ref.group(1):
                        col = col * 26 + (ord(ch) - 64)
                    col -= 1
                else:
                    col = max(cells.keys(), default=-1) + 1
                if col >= MAX_TABLE_COLS:
                    continue
                v = re.search(r"<v>(.*?)</v>", body, re.S)
                if 't="s"' in attrs and v:
                    try:
                        val = shared[int(v.group(1))]
                    except (ValueError, IndexError):
                        val = v.group(1)
                elif 't="inlineStr"' in attrs:
                    val = "".join(html.unescape(re.sub(r"<[^>]+>", "", t))
                                  for t in re.findall(r"<t[^>]*>(.*?)</t>", body, re.S))
                else:
                    val = html.unescape(v.group(1)) if v else ""
                cells[col] = val[:MAX_CELL_CHARS]
            width = max(cells.keys(), default=-1) + 1
            rows.append([cells.get(i, "") for i in range(min(width, MAX_TABLE_COLS))])
        if rows:
            return rows
    return []


def _rtf_text(fp: str) -> tuple[str, bool]:
    with open(fp, "rb") as f:
        raw = f.read(MAX_SCAN_BYTES)
    raw = re.sub(rb"\\'([0-9a-fA-F]{2})", lambda m: bytes([int(m.group(1), 16)]), raw)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("gb18030", errors="replace")
    text = re.sub(r"\\par[d]?\b", "\n", text)
    text = re.sub(r"\\tab\b", "\t", text)
    text = re.sub(r"\\[a-zA-Z]+-?\d*\s?", "", text)
    return text.replace("{", "").replace("}", "")[:MAX_CHARS], os.path.getsize(fp) > MAX_SCAN_BYTES


def build_preview(fp: str, *, ext: str = "", kind: str = "", name: str = "",
                  offset: int = 0, limit: int = MAX_CHARS) -> dict:
    """生成单文件预览 JSON。只读取有界字节,任何异常都降级为 hex 视图而非 500。"""
    e = norm_ext(ext or fp)
    size = os.path.getsize(fp)
    offset = max(0, min(int(offset or 0), MAX_OFFSET))
    limit = max(256, min(int(limit or MAX_CHARS), MAX_CHARS))
    with open(fp, "rb") as f:
        sample = f.read(HEX_BYTES)
    mode, native, reason = mode_for(e, kind, sample)
    out = {
        "ext": e, "name": name or os.path.basename(fp), "size": size,
        "mode": mode, "native": native, "reason": reason,
        "encoding": "", "text": "", "rows": [], "sections": [],
        "chars": 0, "lines": 0, "offset": offset,
        "next_offset": None, "truncated": False,
    }
    try:
        if mode in ("image", "video", "audio", "pdf", "font"):
            return out
        enc = detect_encoding(sample)
        out["encoding"] = enc
        if mode == "subtitle" or (mode in ("text", "code") and e != "rtf" and e != "json"):
            text, nxt, more = _read_text(fp, enc, offset, limit)
            out.update(text=text, next_offset=nxt, truncated=more,
                       chars=len(text), lines=text.count("\n") + (1 if text else 0))
            return out
        if e == "rtf":
            text, truncated = _rtf_text(fp)
            out.update(text=text, truncated=truncated, chars=len(text),
                       lines=text.count("\n") + 1)
            return out
        if e == "json":
            raw, truncated = _scan_text(fp, enc)
            text, truncated = _pretty_json(raw, truncated)
            out.update(text=text, truncated=truncated, chars=len(text),
                       lines=text.count("\n") + 1)
            return out
        if mode == "table":
            rows, truncated = ((_xlsx_rows(fp), False) if e in ZIP_TABLE_EXTS
                               else _csv_rows(fp, enc, e))
            out.update(rows=rows, truncated=truncated)
            return out
        if mode == "sections":
            if e.startswith(("pptx", "pptm", "odp")):
                sections = _pptx_sections(fp)
            elif e == "epub":
                sections = _epub_sections(fp)
            else:
                sections = _docx_sections(fp)
            used = sum(len(s["text"]) for s in sections)
            out.update(sections=sections, truncated=used >= MAX_CHARS)
            return out
    except (OSError, UnicodeError, ValueError, KeyError, IndexError):
        out.update(mode="hex", native=False, reason="parse_error")
    dump = hex_dump(sample)
    out.update(text=dump, chars=len(dump), lines=len(dump.splitlines()))
    return out


def content_disposition(kind: str, name: str) -> str:
    """RFC 5987 双文件名;去掉 CRLF/引号防响应头注入。"""
    raw = re.sub(r'[\r\n"\\]', "_", name or "file")[:120] or "file"
    ascii_name = re.sub(r"[^\x20-\x7e]", "_", raw)
    quoted = urllib.parse.quote(raw, safe="")
    return f'{kind}; filename="{ascii_name}"; filename*=UTF-8\'\'{quoted}'


def response_headers(fp: str, ext: str, dest: str = "") -> tuple[str, str, str | None]:
    """/api/file 的 (Content-Type, Content-Disposition, CSP)。

    * 顶层导航打开 HTML/SVG/JS/XML → text/plain + attachment + nosniff,脚本不执行;
    * SVG 只在 Sec-Fetch-Dest=image(<img> 上下文)作为 image/svg+xml 静态渲染,
      并额外下发 CSP sandbox;
    * 其余按扩展名白名单给准确 MIME,可预览的 inline,其余 attachment;
    * 文本按探测到的编码声明 charset,中文 GBK 文件在新标签页也不乱码。
    """
    e = norm_ext(ext or fp)
    with open(fp, "rb") as f:
        head = f.read(4096)
    enc = detect_encoding(head)
    ctype = mime_for(e)
    if ctype.startswith("text/") or ctype in ("application/json", "application/xml",
                                               "application/x-ndjson", "model/gltf+json"):
        ctype += "; charset=" + charset_label(enc)
    csp = None
    if e in ACTIVE_EXTS:
        if e == "svg" and dest == "image":
            ctype = "image/svg+xml"
            kind = "inline"
            csp = "sandbox; default-src 'none'"
        else:
            ctype = "text/plain; charset=" + charset_label(enc)
            kind = "attachment"
            csp = "sandbox; default-src 'none'"
    else:
        kind = "inline" if e in INLINE_EXTS else "attachment"
    return ctype, content_disposition(kind, os.path.basename(fp)), csp
