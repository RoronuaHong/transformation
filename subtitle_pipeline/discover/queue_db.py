from __future__ import annotations

import json
import sqlite3
import threading
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

CREATE TABLE IF NOT EXISTS job_steps (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL,
  step_key TEXT NOT NULL,
  kind TEXT NOT NULL,
  lang TEXT,
  status TEXT NOT NULL DEFAULT 'queued',
  attempts INTEGER NOT NULL DEFAULT 0,
  qa_status TEXT,
  error TEXT,
  artifact_rel TEXT,
  started_at REAL,
  finished_at REAL,
  UNIQUE(job_id, step_key)
);

CREATE INDEX IF NOT EXISTS idx_job_steps_job
  ON job_steps(job_id, status);
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
        # Language workers share one QueueDB for job_steps; allow cross-thread
        # use and serialize writes with ``_lock``.
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=5000")
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
        meta: dict | None = None,
    ) -> str:
        """Insert job. Returns inserted | ignored | skipped_done.

        When ``meta`` is provided it is written in the same INSERT commit so
        schedulers cannot claim a pending row before depends_on gates exist.
        """
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
        if isinstance(meta, dict) and meta:
            base = dict(c.raw) if isinstance(c.raw, dict) else {}
            base.update(meta)
            meta_json = json.dumps(base, ensure_ascii=False)
        elif c.raw:
            meta_json = json.dumps(c.raw, ensure_ascii=False)
        else:
            meta_json = None
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
                    meta_json,
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

    def list_video_jobs(self, platform: str, video_id: str) -> list[sqlite3.Row]:
        """All workflow rows for one (platform, video_id) pair."""
        return list(
            self._conn.execute(
                "SELECT * FROM jobs WHERE platform=? AND video_id=? ORDER BY id ASC",
                (platform, video_id),
            )
        )

    def delete_video_jobs(self, platform: str, video_id: str) -> int:
        """Remove a video from the queue completely. Returns deleted row count."""
        cur = self._conn.execute(
            "DELETE FROM jobs WHERE platform=? AND video_id=?", (platform, video_id)
        )
        self._conn.commit()
        return int(cur.rowcount)

    def delete_job(self, job_id: int, *, allow_statuses: tuple[str, ...] | None = None) -> int:
        """Remove one job (and its steps). Files/notes are kept.

        ``allow_statuses`` makes the delete conditional, so a job that got
        requeued between the UI check and this call is never removed.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT status FROM jobs WHERE id=?", (int(job_id),)
            ).fetchone()
            if row is None:
                return 0
            if allow_statuses is not None and str(row["status"] or "") not in allow_statuses:
                return 0
            self._conn.execute("DELETE FROM job_steps WHERE job_id=?", (int(job_id),))
            cur = self._conn.execute("DELETE FROM jobs WHERE id=?", (int(job_id),))
            self._conn.commit()
            return int(cur.rowcount)

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
    ) -> bool:
        """Mark done only from ``processing`` (or already done). Never overwrite cancelled."""
        now = time.time()
        cur = self._conn.execute(
            """
            UPDATE jobs
            SET status='done', updated_at=?, last_error=NULL,
                source_wav=COALESCE(?, source_wav),
                canonical_url=COALESCE(?, canonical_url)
            WHERE id=? AND status IN ('processing', 'done', 'partial')
            """,
            (now, source_wav, canonical_url, job_id),
        )
        self._conn.commit()
        return cur.rowcount == 1

    def set_status(self, job_id: int, status: str) -> bool:
        """Set an explicit lifecycle status (e.g. ``awaiting_confirm``).

        Claiming only selects ``pending`` rows, so a job parked here will not be
        picked up again until an operator confirms and requeues it.
        """
        allowed = {"pending", "processing", "awaiting_confirm", "partial"}
        if status not in allowed:
            raise ValueError(f"unsupported status: {status}")
        cur = self._conn.execute(
            "UPDATE jobs SET status=?, updated_at=? WHERE id=? AND status != 'cancelled'",
            (status, time.time(), job_id),
        )
        self._conn.commit()
        return cur.rowcount == 1

    def mark_failed(self, job_id: int, error: str, *, dead: bool = False) -> bool:
        """Fail only non-cancelled/finished rows (cancel must stick).

        ``pending`` is allowed for step-aggregate paths; submit/retry must not
        reopen a row while its lane slot is still held (zombie guard).
        """
        now = time.time()
        status = "dead" if dead else "failed"
        cur = self._conn.execute(
            """
            UPDATE jobs
            SET status=?, last_error=?, updated_at=?
            WHERE id=? AND status NOT IN ('cancelled', 'done', 'published')
            """,
            (status, (error or "")[:2000], now, job_id),
        )
        self._conn.commit()
        return cur.rowcount == 1

    def mark_partial(self, job_id: int, *, error: str | None = None) -> bool:
        """Parent job finished with some usable artifacts but failed/review steps."""
        now = time.time()
        cur = self._conn.execute(
            """
            UPDATE jobs
            SET status='partial', last_error=?, updated_at=?
            WHERE id=? AND status NOT IN ('cancelled', 'done', 'published')
            """,
            ((error or "")[:2000] or None, now, job_id),
        )
        self._conn.commit()
        return cur.rowcount == 1

    # --- job_steps (P2 language-level accounting) ---

    def upsert_step(
        self,
        job_id: int,
        step_key: str,
        *,
        kind: str,
        lang: str | None = None,
        status: str = "queued",
        qa_status: str | None = None,
        error: str | None = None,
        artifact_rel: str | None = None,
    ) -> None:
        now = time.time()
        key = (step_key or "").strip()
        if not key:
            return
        with self._lock:
            row = self.get_step(job_id, key)
            if row is None:
                self._conn.execute(
                    """
                    INSERT INTO job_steps(
                      job_id, step_key, kind, lang, status, attempts,
                      qa_status, error, artifact_rel, started_at, finished_at
                    ) VALUES (?,?,?,?,?,0,?,?,?,?,?)
                    """,
                    (
                        int(job_id),
                        key,
                        (kind or "translate").strip() or "translate",
                        (lang or None),
                        status,
                        qa_status,
                        (error or None),
                        artifact_rel,
                        now if status == "running" else None,
                        now
                        if status in ("done", "failed", "skipped", "partial")
                        else None,
                    ),
                )
            else:
                if status == "running" and self._job_is_cancelled(job_id):
                    return
                self._conn.execute(
                    """
                    UPDATE job_steps
                    SET kind=?, lang=COALESCE(?, lang), status=?,
                        qa_status=COALESCE(?, qa_status),
                        error=?, artifact_rel=COALESCE(?, artifact_rel),
                        started_at=CASE
                          WHEN ?='running' THEN COALESCE(started_at, ?)
                          ELSE started_at
                        END,
                        finished_at=CASE
                          WHEN ? IN ('done','failed','skipped','partial') THEN ?
                          ELSE finished_at
                        END
                    WHERE job_id=? AND step_key=?
                    """,
                    (
                        (kind or row["kind"] or "translate"),
                        lang,
                        status,
                        qa_status,
                        (error or None),
                        artifact_rel,
                        status,
                        now,
                        status,
                        now,
                        int(job_id),
                        key,
                    ),
                )
            self._conn.commit()

    def get_step(self, job_id: int, step_key: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM job_steps WHERE job_id=? AND step_key=?",
                (int(job_id), (step_key or "").strip()),
            ).fetchone()

    def list_steps(self, job_id: int) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self._conn.execute(
                    """
                    SELECT * FROM job_steps
                    WHERE job_id=?
                    ORDER BY id ASC
                    """,
                    (int(job_id),),
                )
            )

    def mark_step_running(self, job_id: int, step_key: str) -> None:
        if self._job_is_cancelled(job_id):
            return
        now = time.time()
        with self._lock:
            self._conn.execute(
                """
                UPDATE job_steps
                SET status='running', started_at=COALESCE(started_at, ?),
                    finished_at=NULL, error=NULL
                WHERE job_id=? AND step_key=?
                """,
                (now, int(job_id), (step_key or "").strip()),
            )
            self._conn.commit()

    def mark_step_done(
        self,
        job_id: int,
        step_key: str,
        *,
        qa_status: str | None = None,
        artifact_rel: str | None = None,
        status: str = "done",
    ) -> None:
        if self._job_is_cancelled(job_id):
            return
        now = time.time()
        st = status if status in ("done", "skipped", "partial") else "done"
        with self._lock:
            self._conn.execute(
                """
                UPDATE job_steps
                SET status=?, qa_status=COALESCE(?, qa_status),
                    artifact_rel=COALESCE(?, artifact_rel),
                    error=NULL, finished_at=?
                WHERE job_id=? AND step_key=?
                """,
                (
                    st,
                    qa_status,
                    artifact_rel,
                    now,
                    int(job_id),
                    (step_key or "").strip(),
                ),
            )
            self._conn.commit()

    def mark_step_failed(
        self, job_id: int, step_key: str, error: str, *, qa_status: str | None = None
    ) -> None:
        if self._job_is_cancelled(job_id):
            return
        now = time.time()
        with self._lock:
            self._conn.execute(
                """
                UPDATE job_steps
                SET status='failed', error=?, qa_status=COALESCE(?, qa_status),
                    finished_at=?
                WHERE job_id=? AND step_key=?
                """,
                (
                    (error or "")[:2000],
                    qa_status,
                    now,
                    int(job_id),
                    (step_key or "").strip(),
                ),
            )
            self._conn.commit()

    def requeue_step(self, job_id: int, step_key: str) -> bool:
        """Reset one step to queued and bump attempts. Returns True if found."""
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE job_steps
                SET status='queued', attempts=attempts+1, error=NULL,
                    qa_status=NULL, started_at=NULL, finished_at=NULL
                WHERE job_id=? AND step_key=?
                """,
                (int(job_id), (step_key or "").strip()),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def delete_steps_for_job(self, job_id: int) -> int:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM job_steps WHERE job_id=?", (int(job_id),)
            )
            self._conn.commit()
            return int(cur.rowcount)

    def reset_running_steps_to_queued(self, *, job_id: int | None = None) -> int:
        """Crash recovery: orphan running steps → queued."""
        with self._lock:
            if job_id is None:
                cur = self._conn.execute(
                    """
                    UPDATE job_steps
                    SET status='queued', started_at=NULL, finished_at=NULL
                    WHERE status='running'
                    """
                )
            else:
                cur = self._conn.execute(
                    """
                    UPDATE job_steps
                    SET status='queued', started_at=NULL, finished_at=NULL
                    WHERE status='running' AND job_id=?
                    """,
                    (int(job_id),),
                )
            self._conn.commit()
            return int(cur.rowcount)

    def steps_summary(self, job_id: int) -> dict:
        rows = self.list_steps(job_id)
        total = len(rows)
        by_status: dict[str, int] = {}
        failed_keys: list[str] = []
        review_keys: list[str] = []
        done = 0
        for r in rows:
            st = str(r["status"] or "")
            by_status[st] = by_status.get(st, 0) + 1
            if st in ("done", "skipped"):
                done += 1
            if st == "failed":
                failed_keys.append(str(r["step_key"]))
            if str(r["qa_status"] or "") == "needs_review":
                review_keys.append(str(r["step_key"]))
        return {
            "total": total,
            "done": done,
            "by_status": by_status,
            "failed_keys": failed_keys,
            "review_keys": review_keys,
            "label": f"{done}/{total}" if total else "0/0",
        }

    def aggregate_job_from_steps(self, job_id: int) -> str | None:
        """Set parent job status from steps. Returns new status or None if no steps."""
        if self._job_is_cancelled(job_id):
            return "cancelled"
        with self._lock:
            rows = list(
                self._conn.execute(
                    "SELECT * FROM job_steps WHERE job_id=? ORDER BY id ASC",
                    (int(job_id),),
                )
            )
        if not rows:
            return None
        statuses = [str(r["status"] or "") for r in rows]
        if any(s == "running" for s in statuses):
            return "processing"
        if any(s == "queued" for s in statuses):
            return None
        n_done = sum(1 for s in statuses if s in ("done", "skipped"))
        n_fail = sum(1 for s in statuses if s == "failed")
        n_partial = sum(1 for s in statuses if s == "partial")
        # needs_review stays on the step/chip only — does not demote the parent.
        if n_fail == 0 and n_partial == 0:
            self.mark_done(job_id)
            return "done"
        if n_done == 0 and n_fail > 0:
            keys = [str(r["step_key"]) for r in rows if r["status"] == "failed"]
            self.mark_failed(
                job_id, f"all language steps failed: {', '.join(keys[:8])}"
            )
            return "failed"
        bad = [
            str(r["step_key"])
            for r in rows
            if r["status"] in ("failed", "partial")
        ]
        self.mark_partial(
            job_id, error=f"partial: {', '.join(bad[:12])}" if bad else None
        )
        return "partial"

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

    def seal_cancelled_steps(self, job_id: int, *, reason: str = "用户取消") -> int:
        """Force unfinished steps to failed after cancel (even if job already cancelled)."""
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE job_steps
                SET status='failed', error=?, finished_at=?
                WHERE job_id=? AND status IN ('queued', 'running')
                """,
                ((reason or "用户取消")[:2000], now, int(job_id)),
            )
            self._conn.commit()
            return int(cur.rowcount)

    def _job_is_cancelled(self, job_id: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT status FROM jobs WHERE id=?", (int(job_id),)
            ).fetchone()
        return bool(row) and str(row["status"] or "") == "cancelled"

    def cancel_job(self, job_id: int, *, reason: str = "用户取消") -> bool:
        """Mark pending/processing job as cancelled."""
        now = time.time()
        cur = self._conn.execute(
            """
            UPDATE jobs
            SET status='cancelled', last_error=?, updated_at=?
            WHERE id=? AND status IN ('pending', 'processing', 'partial')
            """,
            ((reason or "用户取消")[:2000], now, job_id),
        )
        self._conn.commit()
        # Always seal steps — even when the row was already cancelled.
        self.seal_cancelled_steps(job_id, reason=reason or "用户取消")
        return cur.rowcount == 1

    def requeue_job(self, job_id: int, *, allow_done: bool = False) -> bool:
        """Reset a failed/dead job to pending (try-page resubmit).

        With ``allow_done=True``, also reopens done/published (e.g. change langs).
        """
        now = time.time()
        allowed = ("failed", "dead", "cancelled", "partial", "awaiting_confirm")
        if allow_done:
            allowed = (
                "failed", "dead", "cancelled", "partial", "awaiting_confirm",
                "done", "published",
            )
        # Reset created_at too so ETA / "age" reflect this run, not the first enqueue.
        cur = self._conn.execute(
            f"""
            UPDATE jobs
            SET status='pending', last_error=NULL, attempts=0,
                created_at=?, updated_at=?
            WHERE id=? AND status IN ({",".join("?" for _ in allowed)})
            """,
            (now, now, job_id, *allowed),
        )
        self._conn.commit()
        if cur.rowcount == 1:
            # Fresh run: clear language steps so resume/skip logic is clean.
            self.delete_steps_for_job(job_id)
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
