"""Content library: published multilingual articles for the site."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

DEFAULT_DB = Path(__file__).resolve().parents[1] / "data" / "content.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  platform TEXT NOT NULL,
  video_id TEXT NOT NULL,
  topic_id TEXT NOT NULL,
  slug TEXT NOT NULL,
  canonical_url TEXT NOT NULL,
  embed_url TEXT,
  source_lang TEXT,
  title_src TEXT,
  author TEXT,
  published_at REAL,
  views INTEGER,
  duration_sec INTEGER,
  thumb_url TEXT,
  source_wav TEXT,
  work_dir TEXT,
  status TEXT NOT NULL DEFAULT 'draft',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE(platform, video_id),
  UNIQUE(topic_id, slug)
);

CREATE TABLE IF NOT EXISTS article_locales (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  article_id INTEGER NOT NULL,
  locale TEXT NOT NULL,
  title TEXT,
  one_liner TEXT,
  summary TEXT,
  outline_json TEXT,
  srt_path TEXT,
  txt_path TEXT,
  summary_json_path TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE(article_id, locale),
  FOREIGN KEY(article_id) REFERENCES articles(id)
);

CREATE INDEX IF NOT EXISTS idx_articles_topic ON articles(topic_id, status);
CREATE INDEX IF NOT EXISTS idx_locales_locale ON article_locales(locale);
"""

_EXTRA_LOCALE_COLUMNS: dict[str, str] = {
    "keypoints_intro": "TEXT",
    "key_points_json": "TEXT",
    "focuses_json": "TEXT",
    "hard_points_json": "TEXT",
}


def _json_list(data: dict[str, Any], key: str, json_key: str) -> str:
    val = data.get(key)
    if isinstance(val, list):
        return json.dumps(val, ensure_ascii=False)
    return str(data.get(json_key) or "[]")


def slugify(text: str, *, fallback: str = "clip") -> str:
    s = (text or "").strip().lower()
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)
    s = re.sub(r"[\s_]+", "-", s).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    # Next.js routes + SEO: prefer ASCII slugs; CJK titles fall back to video id.
    if not s or len(re.sub(r"[^a-z0-9-]", "", s)) < 2:
        s = re.sub(r"[^a-z0-9-]", "", fallback.lower()) or "clip"
    return s[:80]


def public_slug(video_id: str, title: str | None = None) -> str:
    """Stable site slug: video_id wins over synthetic try:/upload: titles."""
    t = (title or "").strip()
    vid = (video_id or "").strip() or "clip"
    if not t or t.startswith("try:") or t.startswith("upload:"):
        return slugify(vid, fallback=vid[:12])
    return slugify(t, fallback=vid)


def embed_url_for(platform: str, video_id: str) -> str:
    if platform == "youtube":
        return f"https://www.youtube.com/embed/{video_id}"
    if platform == "bilibili":
        return (
            f"https://player.bilibili.com/player.html?bvid={video_id}"
            f"&high_quality=1&danmaku=0"
        )
    return ""


class ContentDB:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else DEFAULT_DB
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        existing = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(article_locales)")
        }
        for col, typ in _EXTRA_LOCALE_COLUMNS.items():
            if col not in existing:
                self._conn.execute(
                    f"ALTER TABLE article_locales ADD COLUMN {col} {typ}"
                )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def upsert_article(self, data: dict[str, Any]) -> int:
        now = time.time()
        row = self._conn.execute(
            "SELECT id, slug FROM articles WHERE platform=? AND video_id=?",
            (data["platform"], data["video_id"]),
        ).fetchone()
        slug = data.get("slug") or slugify(
            str(data.get("title_src") or ""), fallback=str(data["video_id"])[:12]
        )
        if row is not None:
            # keep existing slug if taken elsewhere conflict avoided
            slug = row["slug"]
            self._conn.execute(
                """
                UPDATE articles SET
                  topic_id=?, canonical_url=?, embed_url=?, source_lang=?,
                  title_src=?, author=?, published_at=?, views=?, duration_sec=?,
                  thumb_url=?, source_wav=?, work_dir=?, status=?, updated_at=?
                WHERE id=?
                """,
                (
                    data["topic_id"],
                    data["canonical_url"],
                    data.get("embed_url"),
                    data.get("source_lang"),
                    data.get("title_src"),
                    data.get("author"),
                    data.get("published_at"),
                    data.get("views"),
                    data.get("duration_sec"),
                    data.get("thumb_url"),
                    data.get("source_wav"),
                    data.get("work_dir"),
                    data.get("status") or "ready",
                    now,
                    row["id"],
                ),
            )
            self._conn.commit()
            return int(row["id"])

        # ensure unique slug per topic
        base = slug
        n = 2
        while self._conn.execute(
            "SELECT 1 FROM articles WHERE topic_id=? AND slug=?",
            (data["topic_id"], slug),
        ).fetchone():
            slug = f"{base}-{n}"
            n += 1

        cur = self._conn.execute(
            """
            INSERT INTO articles(
              platform, video_id, topic_id, slug, canonical_url, embed_url,
              source_lang, title_src, author, published_at, views, duration_sec,
              thumb_url, source_wav, work_dir, status, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                data["platform"],
                data["video_id"],
                data["topic_id"],
                slug,
                data["canonical_url"],
                data.get("embed_url") or embed_url_for(data["platform"], data["video_id"]),
                data.get("source_lang"),
                data.get("title_src"),
                data.get("author"),
                data.get("published_at"),
                data.get("views"),
                data.get("duration_sec"),
                data.get("thumb_url"),
                data.get("source_wav"),
                data.get("work_dir"),
                data.get("status") or "ready",
                now,
                now,
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def upsert_locale(self, article_id: int, locale: str, data: dict[str, Any]) -> None:
        now = time.time()
        outline = data.get("outline")
        outline_json = (
            json.dumps(outline, ensure_ascii=False)
            if isinstance(outline, list)
            else (data.get("outline_json") or "[]")
        )
        key_points_json = _json_list(data, "key_points", "key_points_json")
        focuses_json = _json_list(data, "focuses", "focuses_json")
        hard_points_json = _json_list(data, "hard_points", "hard_points_json")
        existing = self._conn.execute(
            "SELECT id FROM article_locales WHERE article_id=? AND locale=?",
            (article_id, locale),
        ).fetchone()
        if existing:
            self._conn.execute(
                """
                UPDATE article_locales SET
                  title=?, one_liner=?, summary=?, outline_json=?,
                  keypoints_intro=?, key_points_json=?,
                  focuses_json=?, hard_points_json=?,
                  srt_path=?, txt_path=?, summary_json_path=?, updated_at=?
                WHERE id=?
                """,
                (
                    data.get("title"),
                    data.get("one_liner"),
                    data.get("summary"),
                    outline_json,
                    data.get("keypoints_intro") or "",
                    key_points_json,
                    focuses_json,
                    hard_points_json,
                    data.get("srt_path"),
                    data.get("txt_path"),
                    data.get("summary_json_path"),
                    now,
                    existing["id"],
                ),
            )
        else:
            self._conn.execute(
                """
                INSERT INTO article_locales(
                  article_id, locale, title, one_liner, summary, outline_json,
                  keypoints_intro, key_points_json, focuses_json, hard_points_json,
                  srt_path, txt_path, summary_json_path, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    article_id,
                    locale,
                    data.get("title"),
                    data.get("one_liner"),
                    data.get("summary"),
                    outline_json,
                    data.get("keypoints_intro") or "",
                    key_points_json,
                    focuses_json,
                    hard_points_json,
                    data.get("srt_path"),
                    data.get("txt_path"),
                    data.get("summary_json_path"),
                    now,
                    now,
                ),
            )
        self._conn.commit()

    def get_article(self, platform: str, video_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM articles WHERE platform=? AND video_id=?",
            (platform, video_id),
        ).fetchone()

    def list_locales(self, article_id: int) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM article_locales WHERE article_id=? ORDER BY locale",
                (article_id,),
            )
        )

    def count_articles(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0])
