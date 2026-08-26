from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .models import Candidate

DEFAULT_DB = Path(__file__).resolve().parents[1] / "data" / "queue.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  platform TEXT NOT NULL,
  video_id TEXT NOT NULL,
  url TEXT NOT NULL,
  title TEXT,
  topic_id TEXT NOT NULL,
  author TEXT,
  author_id TEXT,
  published_at REAL,
  views INTEGER,
  likes INTEGER,
  comments INTEGER,
  coins INTEGER,
  favorites INTEGER,
  shares INTEGER,
  duration_sec INTEGER,
  thumb_url TEXT,
  description TEXT,
  query TEXT,
  score REAL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'pending',
  priority TEXT NOT NULL DEFAULT 'normal',
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  source_wav TEXT,
  canonical_url TEXT,
  meta_json TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE(platform, video_id)
);

CREATE INDEX IF NOT EXISTS idx_jobs_status_score
  ON jobs(status, priority, score DESC);
"""

# Columns added after first Phase-0 schema; migrate safely.
_EXTRA_COLUMNS: dict[str, str] = {
    "author_id": "TEXT",
    "coins": "INTEGER",
    "favorites": "INTEGER",
    "shares": "INTEGER",
    "description": "TEXT",
    "query": "TEXT",
    "meta_json": "TEXT",
    "original_url": "TEXT",
}


class QueueDB:
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
            r[1]
            for r in self._conn.execute("PRAGMA table_info(jobs)").fetchall()
        }
        for col, typ in _EXTRA_COLUMNS.items():
            if col not in existing:
                self._conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {typ}")

    def close(self) -> None:
        self._conn.close()

    def enqueue(
        self,
        c: Candidate,
        *,
        priority: str = "normal",
        status: str = "pending",
    ) -> str:
        """Insert job. Returns inserted | ignored | skipped_done."""
        row = self._conn.execute(
            "SELECT status FROM jobs WHERE platform=? AND video_id=?",
            (c.platform, c.video_id),
        ).fetchone()
        if row is not None:
            st = row["status"]
            if st in ("done", "published", "processing"):
                return "skipped_done"
            return "ignored"

        now = time.time()
        meta = json.dumps(c.raw or {}, ensure_ascii=False) if c.raw else None
        try:
            self._conn.execute(
                """
                INSERT INTO jobs(
                  platform, video_id, url, title, topic_id, author, author_id,
                  published_at, views, likes, comments, coins, favorites, shares,
                  duration_sec, thumb_url, description, query,
                  score, status, priority, attempts, canonical_url, meta_json,
                  original_url, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    c.platform,
                    c.video_id,
                    c.url,
                    c.title,
                    c.topic_id,
                    c.author,
                    c.author_id,
                    c.published_at,
                    c.views,
                    c.likes,
                    c.comments,
                    c.coins,
                    c.favorites,
                    c.shares,
                    c.duration_sec,
                    c.thumb_url,
                    c.description,
                    c.query,
                    float(c.score),
                    status,
                    priority,
                    0,
                    c.url,
                    meta,
                    c.original_url or c.url,
                    now,
                    now,
                ),
            )
            self._conn.commit()
            return "inserted"
        except sqlite3.IntegrityError:
            return "ignored"

    def list_pending(self, *, limit: int = 50) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                """
                SELECT * FROM jobs
                WHERE status='pending'
                ORDER BY
                  CASE priority WHEN 'high' THEN 0 ELSE 1 END,
                  score DESC,
                  created_at ASC
                LIMIT ?
                """,
                (limit,),
            )
        )

    def claim_next(
        self, *, max_attempts: int = 3, video_id: str | None = None
    ) -> sqlite3.Row | None:
        """Atomically move one pending job to processing. Returns the row or None."""
        params: list = [max_attempts]
        extra = ""
        if video_id:
            extra = " AND video_id=?"
            params.append(video_id)
        row = self._conn.execute(
            f"""
            SELECT * FROM jobs
            WHERE status='pending' AND attempts < ?{extra}
            ORDER BY
              CASE priority WHEN 'high' THEN 0 ELSE 1 END,
              score DESC,
              created_at ASC
            LIMIT 1
            """,
            params,
        ).fetchone()
        if row is None:
            return None
        now = time.time()
        cur = self._conn.execute(
            """
            UPDATE jobs
            SET status='processing', attempts=attempts+1, updated_at=?, last_error=NULL
            WHERE id=? AND status='pending'
            """,
            (now, row["id"]),
        )
        self._conn.commit()
        if cur.rowcount != 1:
            return None
        return self.get_job(int(row["id"]))

    def get_job(self, job_id: int) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()

    def mark_done(
        self,
        job_id: int,
        *,
        source_wav: str | None = None,
        canonical_url: str | None = None,
    ) -> None:
        now = time.time()
        self._conn.execute(
            """
            UPDATE jobs
            SET status='done', updated_at=?, last_error=NULL,
                source_wav=COALESCE(?, source_wav),
                canonical_url=COALESCE(?, canonical_url)
            WHERE id=?
            """,
            (now, source_wav, canonical_url, job_id),
        )
        self._conn.commit()

    def mark_failed(self, job_id: int, error: str, *, dead: bool = False) -> None:
        now = time.time()
        status = "dead" if dead else "failed"
        self._conn.execute(
            """
            UPDATE jobs
            SET status=?, last_error=?, updated_at=?
            WHERE id=?
            """,
            (status, (error or "")[:2000], now, job_id),
        )
        self._conn.commit()

    def requeue_failed(self, *, limit: int = 20) -> int:
        """Move failed → pending for retry. Returns count."""
        now = time.time()
        cur = self._conn.execute(
            """
            UPDATE jobs SET status='pending', updated_at=?, last_error=NULL
            WHERE id IN (
              SELECT id FROM jobs WHERE status='failed' ORDER BY updated_at ASC LIMIT ?
            )
            """,
            (now, limit),
        )
        self._conn.commit()
        return int(cur.rowcount)

    def requeue_job(self, job_id: int, *, allow_done: bool = False) -> bool:
        """Reset a failed/dead job to pending (try-page resubmit).

        With ``allow_done=True``, also reopens done/published (e.g. change langs).
        """
        now = time.time()
        allowed = ("failed", "dead")
        if allow_done:
            allowed = ("failed", "dead", "done", "published")
        cur = self._conn.execute(
            f"""
            UPDATE jobs
            SET status='pending', last_error=NULL, attempts=0, updated_at=?
            WHERE id=? AND status IN ({",".join("?" for _ in allowed)})
            """,
            (now, job_id, *allowed),
        )
        self._conn.commit()
        return cur.rowcount == 1

    def set_meta(self, job_id: int, meta: dict) -> None:
        """Merge dict into jobs.meta_json."""
        row = self.get_job(job_id)
        if row is None:
            return
        current: dict = {}
        raw = row["meta_json"]
        if raw:
            try:
                current = json.loads(raw)
            except json.JSONDecodeError:
                current = {}
        if not isinstance(current, dict):
            current = {}
        current.update(meta)
        now = time.time()
        self._conn.execute(
            "UPDATE jobs SET meta_json=?, updated_at=? WHERE id=?",
            (json.dumps(current, ensure_ascii=False), now, job_id),
        )
        self._conn.commit()

    def set_source_wav(self, job_id: int, wav: str) -> None:
        now = time.time()
        self._conn.execute(
            "UPDATE jobs SET source_wav=?, updated_at=? WHERE id=?",
            (wav, now, job_id),
        )
        self._conn.commit()

    def count_by_status(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
        ).fetchall()
        return {r["status"]: int(r["n"]) for r in rows}

    def list_jobs(self, *, limit: int = 200) -> list[sqlite3.Row]:
        """Newest first. Used to sync original links into Mongo."""
        return list(
            self._conn.execute(
                "SELECT * FROM jobs ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            )
        )

