"""MCP resources — read-only export/API snapshots."""

from __future__ import annotations

import json
from pathlib import Path

from vitual_mcp.client import VitualApiError, VitualClient

_PIPELINE_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = _PIPELINE_ROOT.parent
ARTICLES_JSON = _REPO_ROOT / "transform" / "content" / "articles.json"
_client = VitualClient()


def _read_articles_raw() -> dict:
    if not ARTICLES_JSON.is_file():
        raise FileNotFoundError(
            f"Export not found: {ARTICLES_JSON}. Run: cd subtitle_pipeline && yarn export-site"
        )
    return json.loads(ARTICLES_JSON.read_text(encoding="utf-8"))


def articles_full() -> str:
    return json.dumps(_read_articles_raw(), ensure_ascii=False, indent=2)


def articles_index() -> str:
    data = _read_articles_raw()
    rows = []
    for a in data.get("articles") or []:
        if not isinstance(a, dict):
            continue
        locales = a.get("locales") if isinstance(a.get("locales"), dict) else {}
        zh = locales.get("zh") if isinstance(locales.get("zh"), dict) else {}
        en = locales.get("en") if isinstance(locales.get("en"), dict) else {}
        title = (
            (zh.get("title") if zh else None)
            or (en.get("title") if en else None)
            or a.get("title_src")
            or a.get("slug")
        )
        rows.append(
            {
                "slug": a.get("slug"),
                "topic": a.get("topic"),
                "platform": a.get("platform"),
                "video_id": a.get("video_id"),
                "source_lang": a.get("source_lang"),
                "title": title,
                "cue_count": len(a.get("cues") or []),
                "locale_count": len(locales),
                "path": f"/topics/{a.get('topic')}/{a.get('slug')}",
            }
        )
    return json.dumps(
        {"count": len(rows), "path": str(ARTICLES_JSON), "articles": rows},
        ensure_ascii=False,
        indent=2,
    )


def article_by_slug(slug: str) -> str:
    data = _read_articles_raw()
    for a in data.get("articles") or []:
        if isinstance(a, dict) and str(a.get("slug") or "") == slug:
            return json.dumps(a, ensure_ascii=False, indent=2)
    raise FileNotFoundError(f"No article with slug={slug!r} in {ARTICLES_JSON}")


async def api_health() -> str:
    try:
        return json.dumps(await _client.health(), ensure_ascii=False, indent=2)
    except VitualApiError as e:
        return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False, indent=2)
