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
  original_url TEXT,
  workflow TEXT NOT NULL DEFAULT 'all',
  pack_id TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE(platform, video_id, workflow)
);

CREATE INDEX IF NOT EXISTS idx_jobs_status_score
  ON jobs(status, priority, score DESC);

CREATE INDEX IF NOT EXISTS idx_jobs_pack
  ON jobs(pack_id, created_at ASC);
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
    "workflow": "TEXT NOT NULL DEFAULT 'all'",
    "pack_id": "TEXT",
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

        # Backfill pack_id / workflow for legacy rows.
        self._conn.execute(
            """
            UPDATE jobs
            SET workflow=COALESCE(NULLIF(workflow, ''), 'all')
            WHERE workflow IS NULL OR workflow=''
            """
        )
        self._conn.execute(
            """
            UPDATE jobs
            SET pack_id=platform || '_' || video_id
            WHERE pack_id IS NULL OR pack_id=''
            """
        )
        self._ensure_workflow_unique()
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_pack ON jobs(pack_id, created_at ASC)"
        )

    def _unique_covers_workflow(self) -> bool:
        """True when UNIQUE(platform, video_id, workflow) is already in place."""
        for idx in self._conn.execute("PRAGMA index_list(jobs)").fetchall():
            # idx: seq, name, unique, origin, partial
            if not idx[2]:
                continue
            cols = [
                r[2]
                for r in self._conn.execute(f"PRAGMA index_info('{idx[1]}')").fetchall()
            ]
            if cols == ["platform", "video_id", "workflow"]:
                return True
        return False

    def _ensure_workflow_unique(self) -> None:
        """Rebuild jobs table when still UNIQUE(platform, video_id) only."""
        if self._unique_covers_workflow():
            return
        # Detect legacy 2-col unique (sqlite_autoindex or explicit).
        legacy = False
        for idx in self._conn.execute("PRAGMA index_list(jobs)").fetchall():
            if not idx[2]:
                continue
            cols = [
                r[2]
                for r in self._conn.execute(f"PRAGMA index_info('{idx[1]}')").fetchall()
            ]
            if cols == ["platform", "video_id"]:
                legacy = True
                break
        if not legacy:
            # Fresh table from SCHEMA already has 3-col unique, or no unique yet.
            if self._unique_covers_workflow():
                return
            # Table created by older SCHEMA without rebuild path: force rebuild.
            # If CREATE TABLE IF NOT EXISTS left old schema, rebuild.
            # Check table sql:
            row = self._conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='jobs'"
            ).fetchone()
            sql = (row[0] or "") if row else ""
            if "UNIQUE(platform, video_id, workflow)" in sql.replace(" ", ""):
                return
            if "UNIQUE(platform, video_id)" in sql.replace(" ", ""):
                legacy = True
            else:
                return
        if not legacy:
            return

        self._conn.executescript(
            """
            CREATE TABLE jobs_wf (
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
              original_url TEXT,
              workflow TEXT NOT NULL DEFAULT 'all',
              pack_id TEXT,
              created_at REAL NOT NULL,
              updated_at REAL NOT NULL,
              UNIQUE(platform, video_id, workflow)
            );
            INSERT INTO jobs_wf(
              id, platform, video_id, url, title, topic_id, author, author_id,
              published_at, views, likes, comments, coins, favorites, shares,
              duration_sec, thumb_url, description, query, score, status, priority,
              attempts, last_error, source_wav, canonical_url, meta_json, original_url,
              workflow, pack_id, created_at, updated_at
            )
            SELECT
              id, platform, video_id, url, title, topic_id, author, author_id,
              published_at, views, likes, comments, coins, favorites, shares,
              duration_sec, thumb_url, description, query, score, status, priority,
              attempts, last_error, source_wav, canonical_url, meta_json, original_url,
              COALESCE(NULLIF(workflow, ''), 'all'),
              COALESCE(NULLIF(pack_id, ''), platform || '_' || video_id),
              created_at, updated_at
            FROM jobs;
            DROP TABLE jobs;
            ALTER TABLE jobs_wf RENAME TO jobs;
            CREATE INDEX IF NOT EXISTS idx_jobs_status_score
              ON jobs(status, priority, score DESC);
            CREATE INDEX IF NOT EXISTS idx_jobs_pack
              ON jobs(pack_id, created_at ASC);
            """
        )

    def close(self) -> None:
        self._conn.close()

    def enqueue(
        self,
        c: Candidate,
        *,
        priority: str = "normal",
        status: str = "pending",
        workflow: str = "all",
        pack_id: str | None = None,
    ) -> str:
        """Insert job. Returns inserted | ignored | skipped_done."""
        wf = (workflow or "all").strip() or "all"
        pack = (pack_id or f"{c.platform}_{c.video_id}").strip()
        row = self._conn.execute(
            "SELECT status FROM jobs WHERE platform=? AND video_id=? AND workflow=?",
            (c.platform, c.video_id, wf),
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
                  original_url, workflow, pack_id, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                    wf,
                    pack,
                    now,
                    now,
                ),
            )
            self._conn.commit()
            return "inserted"
        except sqlite3.IntegrityError:
            return "ignored"

    def get_job_by_workflow(
        self, platform: str, video_id: str, workflow: str = "all"
    ) -> sqlite3.Row | None:
        return self._conn.execute(
            """
            SELECT * FROM jobs
            WHERE platform=? AND video_id=? AND workflow=?
            """,
            (platform, video_id, workflow),
        ).fetchone()

    def list_pack_jobs(self, pack_id: str) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                """
                SELECT * FROM jobs
                WHERE pack_id=?
                ORDER BY created_at ASC, id ASC
                """,
                (pack_id,),
            )
        )

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
        self,
        *,
        max_attempts: int = 3,
        video_id: str | None = None,
        job_id: int | None = None,
    ) -> sqlite3.Row | None:
        """Atomically move one pending job to processing. Returns the row or None."""
        params: list = [max_attempts]
        extra = ""
        if job_id is not None:
            extra = " AND id=?"
            params.append(job_id)
        elif video_id:
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

    def cancel_job(self, job_id: int, *, reason: str = "用户取消") -> bool:
        """Mark pending/processing job as cancelled."""
        now = time.time()
        cur = self._conn.execute(
            """
            UPDATE jobs
            SET status='cancelled', last_error=?, updated_at=?
            WHERE id=? AND status IN ('pending', 'processing')
            """,
            ((reason or "用户取消")[:2000], now, job_id),
        )
        self._conn.commit()
        return cur.rowcount == 1

    def requeue_job(self, job_id: int, *, allow_done: bool = False) -> bool:
        """Reset a failed/dead job to pending (try-page resubmit).

        With ``allow_done=True``, also reopens done/published (e.g. change langs).
        """
        now = time.time()
        allowed = ("failed", "dead", "cancelled")
        if allow_done:
            allowed = ("failed", "dead", "cancelled", "done", "published")
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
