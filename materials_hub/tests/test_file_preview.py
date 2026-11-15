# -*- coding: utf-8 -*-
"""全格式内容预览回归:txt 不再只显示 .TXT、编码/分页、Office 提取、
不可信内容不执行、MIME/Disposition/nosniff 安全头、二进制降级视图。

依据:OWASP File Upload Cheat Sheet、ASVS 5.0 V3.2.1、MDN
Content-Disposition / X-Content-Type-Options / iframe sandbox(PDF 不能加 sandbox)。
"""
import io
import json
import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core  # noqa: E402
import server  # noqa: E402
import file_preview as pv  # noqa: E402


class _FakeHandler:
    """只实现 server.Handler.do_GET 用到的 handler 协议,不起 socket。"""

    def __init__(self, path, headers=None):
        self.path = path
        self.headers = headers or {}
        self.code = None
        self.sent = {}
        self.wfile = io.BytesIO()
        self.payload = None
        self.body = None
        # 复用真实 _serve_file,验证它写出的全套响应头(Range/ETag/nosniff/Disposition)
        self._serve_file = (
            lambda fp, ctype, head_only=False, disposition=None, csp=None:
            server.Handler._serve_file(self, fp, ctype, head_only, disposition, csp)
        )

    def send_response(self, code, *_args):
        self.code = code

    def send_header(self, key, value):
        self.sent[key.lower()] = value

    def end_headers(self):
        return None

    def _maybe_gateway(self):
        return False

    def _json(self, obj, code=200):
        self.payload = obj
        self.code = code
        return obj

    def _send(self, code, body, ctype="application/json"):
        self.code, self.body = code, body
        return body


def _write(tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


def _http(monkeypatch, tmp_path, filename, path, headers=None, kind="docs", ext=None):
    """把临时文件登记成内部素材,用假 handler 走真实 do_GET 分支。"""
    fp = str(tmp_path / filename) if (tmp_path / filename).exists() else None
    # 内部素材必须落在 HUB 内(_safe_resolve 防穿越);测试统一放隔离的 INDEX_DIR。
    if fp is None:
        raise AssertionError("missing fixture file")
    os.makedirs(core.INDEX_DIR, exist_ok=True)
    hub_fp = os.path.join(core.INDEX_DIR, filename)
    with open(fp, "rb") as src, open(hub_fp, "wb") as dst:
        dst.write(src.read())
    mat = {
        "id": "mid", "name": filename, "kind": kind,
        "ext": ext or os.path.splitext(filename)[1],
        "location": "internal", "rel_path": os.path.relpath(hub_fp, server.HUB),
    }
    monkeypatch.setattr(server, "HUB_TOKEN", "")
    monkeypatch.setattr(server, "get_material",
                        lambda mid: mat if mid == "mid" else None)
    h = _FakeHandler(path, headers)
    server.Handler.do_GET(h)
    return h, hub_fp


def test_txt_preview_shows_real_content_and_inline_headers(monkeypatch, tmp_path):
    data = "第一行 485000314\n第二行 中文内容\n".encode("utf-8")
    _write(tmp_path, "note.txt", data)
    h, fp = _http(monkeypatch, tmp_path, "note.txt", "/api/file/mid")
    assert h.code == 200
    assert h.sent["content-type"] == "text/plain; charset=utf-8"
    assert h.sent["x-content-type-options"] == "nosniff"
    assert h.sent["content-disposition"].startswith("inline; filename=")
    assert "filename*=UTF-8''" in h.sent["content-disposition"]
    assert h.sent["cache-control"].startswith("private")
    assert h.wfile.getvalue() == data

    p = _FakeHandler("/api/preview/mid", {"Sec-Fetch-Dest": "empty"})
    server.Handler.do_GET(p)
    assert p.code == 200 and p.payload["mode"] == "text"
    assert "485000314" in p.payload["text"] and "中文内容" in p.payload["text"]
    assert p.payload["encoding"] == "utf-8"
    assert p.payload["lines"] == 3

    r = pv.build_preview(fp, ext=".txt")
    assert r["mode"] == "text" and r["truncated"] is False
    assert r["next_offset"] is None


def test_gbk_txt_no_mojibake_and_pagination_by_chars(tmp_path):
    data = ("中文预览测试" * 12000).encode("gb18030")
    fp = _write(tmp_path, "gbk.txt", data)
    a = pv.build_preview(fp, ext="txt", limit=1024)
    assert a["encoding"] == "gb18030"
    assert a["text"] == "中文预览测试" * 170 + "中文预览"
    assert a["next_offset"] == 1024 and a["truncated"] is True
    b = pv.build_preview(fp, ext="txt", offset=a["next_offset"], limit=1024)
    assert b["text"][0] not in ("\ufffd", "?")
    assert "中文" in b["text"]
    ctype, _, _ = pv.response_headers(fp, ".txt")
    assert ctype == "text/plain; charset=gb18030"


def test_csv_json_and_unknown_text(tmp_path):
    csv_fp = _write(tmp_path, "sheet.csv", "name,count\n苹果,3\n香蕉,5\n".encode("utf-8"))
    rows = pv.build_preview(csv_fp, ext="csv")
    assert rows["mode"] == "table" and rows["rows"][0] == ["name", "count"]
    assert rows["rows"][2] == ["香蕉", "5"]          # 最后一行不能被截断逻辑吃掉

    json_fp = _write(tmp_path, "data.json", json.dumps({"a": 1, "中文": [2, 3]},
                                                     ensure_ascii=False).encode("utf-8"))
    j = pv.build_preview(json_fp, ext="json")
    assert j["mode"] == "text" and '"a": 1' in j["text"] and "中文" in j["text"]

    noext_fp = _write(tmp_path, "Makefile", b"all:\n\techo hello\n")
    n = pv.build_preview(noext_fp, ext="", kind="docs")
    assert n["mode"] == "text" and n["reason"] == "sniffed_text"
    assert "echo hello" in n["text"]


def _zip(path, parts):
    with zipfile.ZipFile(path, "w") as z:
        for name, xml in parts.items():
            z.writestr(name, xml)


def test_office_zip_text_extraction(tmp_path):
    docx = str(tmp_path / "doc.docx")
    _zip(docx, {"word/document.xml":
                "<w:document><w:p><w:t>你好 World</w:t></w:p>"
                "<w:p><w:t>第二行</w:t></w:p></w:document>"})
    d = pv.build_preview(docx, ext="docx")
    assert d["mode"] == "sections" and d["reason"] == "office_text_only"
    assert "你好 World" in d["sections"][0]["text"]
    assert "第二行" in d["sections"][0]["text"]

    pptx = str(tmp_path / "deck.pptx")
    _zip(pptx, {"ppt/slides/slide1.xml": "<p:sld><a:t>标题一</a:t><a:t>正文</a:t></p:sld>"})
    p = pv.build_preview(pptx, ext="pptx")
    assert p["mode"] == "sections" and "标题一" in p["sections"][0]["text"]

    xlsx = str(tmp_path / "book.xlsx")
    _zip(xlsx, {
        "xl/sharedStrings.xml": "<sst><si><t>姓名</t></si><si><t>小明</t></si></sst>",
        "xl/worksheets/sheet1.xml":
            "<worksheet><sheetData><row>"
            '<c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c>'
            "</row><row>"
            '<c r="A2" t="inlineStr"><is><t>年龄</t></is></c><c r="B2"><v>18</v></c>'
            "</row></sheetData></worksheet>",
    })
    x = pv.build_preview(xlsx, ext="xlsx")
    assert x["mode"] == "table" and x["rows"][0] == ["姓名", "小明"]
    assert x["rows"][1] == ["年龄", "18"]

    # 恶意 XML 实体声明:直接跳过该成员,不解析、不做实体展开
    evil = str(tmp_path / "evil.docx")
    _zip(evil, {"word/document.xml":
                '<!DOCTYPE x [<!ENTITY a "boom">]><w:document><w:p><w:t>&a;</w:t></w:p></w:document>'})
    assert pv.build_preview(evil, ext="docx")["sections"] == []


def test_active_content_never_executes_and_secure_headers(tmp_path):
    html_fp = _write(tmp_path, "page.html", b"<html><script>alert(1)</script><b>hello</b></html>")
    h = pv.build_preview(html_fp, ext="html")
    assert h["mode"] == "code" and h["reason"] == "active_source_only"
    assert "<script>" in h["text"]                    # 只作为源码文本返回
    ctype, disp, csp = pv.response_headers(html_fp, ".html", "document")
    assert ctype.startswith("text/plain;") and disp.startswith("attachment;")
    assert csp == "sandbox; default-src 'none'"

    svg_fp = _write(tmp_path, "logo.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>')
    img_type, img_disp, img_csp = pv.response_headers(svg_fp, ".svg", "image")
    assert img_type == "image/svg+xml" and img_disp.startswith("inline;")
    assert img_csp.startswith("sandbox")
    top_type, top_disp, _ = pv.response_headers(svg_fp, ".svg", "document")
    assert top_type.startswith("text/plain;") and top_disp.startswith("attachment;")

    docx_fp = str(tmp_path / "legacy.docx")
    _zip(docx_fp, {"word/document.xml": "<w:document/>"})
    t, d, _ = pv.response_headers(docx_fp, ".docx")
    assert t.startswith("application/vnd.openxmlformats") and d.startswith("attachment;")

    # 文件名里的 CRLF / 引号不能注入响应头
    disp = pv.content_disposition("inline", 'a\r\nX-Evil: 1".txt')
    assert "\r" not in disp and "\n" not in disp and 'X-Evil' in disp


def test_binary_fallback_and_mode_matrix(tmp_path):
    png = _write(tmp_path, "raw.bin", b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR")
    b = pv.build_preview(png, ext="bin")
    assert b["mode"] == "hex" and "89 50 4e 47" in b["text"]
    assert b["reason"] in ("no_renderer", "parse_error")
    assert pv.mode_for("mkv") == ("video", False, "unsupported_container")
    assert pv.mode_for("mp4") == ("video", True, "")
    assert pv.mode_for("doc")[0] == "hex"
    assert pv.mode_for("pdf") == ("pdf", True, "")
    assert pv.mode_for("woff2")[0] == "font"
    assert pv.mode_for("srt")[0] == "subtitle"
    assert pv.mode_for("obj")[0] == "text"
    assert pv.mode_for("svg")[2] == "svg_static"


def test_norm_ext_contract():
    """文档承诺 '.TXT'/'note.TXT'/'txt' 都归一到 'txt'。"""
    for v in (".TXT", "note.TXT", "txt", "/a/b/note.TXT", "note.txt"):
        assert pv.norm_ext(v) == "txt", v


def test_matrix_covers_every_registered_extension(tmp_path):
    """core.KINDS 里登记的每种扩展名都必须有明确预览分支,不能掉回 '.XXX' 占位。"""
    fp = _write(tmp_path, "sample.bin", b"PK\x03\x04\x00\x01\x02\x03 sample")
    allowed = {"image", "video", "audio", "pdf", "font", "subtitle",
               "text", "code", "table", "sections", "hex"}
    covered = 0
    for kind, exts in core.KINDS.items():
        for ext in exts:
            mode, native, _reason = pv.mode_for(ext, kind)
            assert mode in allowed, (kind, ext, mode)
            assert isinstance(native, bool)
            ctype, disp, _csp = pv.response_headers(fp, ext, "document")
            assert ctype and disp
            if ext.lstrip(".") in pv.ACTIVE_EXTS:
                # 同源顶层导航打开时,任何可携带脚本的格式都必须是纯文本 + 下载
                assert ctype.startswith("text/plain;"), (ext, ctype)
                assert disp.startswith("attachment;"), (ext, disp)
            covered += 1
    assert covered > 50


def test_preview_endpoint_rejects_top_level_navigation(monkeypatch, tmp_path):
    _write(tmp_path, "note.txt", b"hello")
    _http(monkeypatch, tmp_path, "note.txt", "/api/file/mid")
    h = _FakeHandler("/api/preview/mid", {
        "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate"})
    server.Handler.do_GET(h)
    assert h.code == 403 and h.payload is None
