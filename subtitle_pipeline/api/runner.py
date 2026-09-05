"""Run discover / batch / export with a process lock; dual-write links to Mongo."""

from __future__ import annotations

import threading
from typing import Any, Callable

from api.mongo import MongoStore
from api.security import emit_alert
from api.settings import Settings
from discover.queue_db import QueueDB

_busy = threading.Lock()
_current: dict[str, str | None] = {"kind": None, "run_id": None}


def current_status() -> dict[str, str | None | bool]:
    return {
        "busy": _busy.locked(),
        "kind": _current.get("kind"),
        "run_id": _current.get("run_id"),
    }


def sync_links_from_queue(store: MongoStore, *, source: str = "pipeline") -> int:
    db = QueueDB()
    try:
        rows = db.list_jobs(limit=500)
        n = 0
        for row in rows:
            orig = row["original_url"] if "original_url" in row.keys() else None
            store.upsert_source_link(
                {
                    "platform": row["platform"],
                    "video_id": row["video_id"],
                    "original_url": orig or row["url"],
                    "canonical_url": row["canonical_url"] or row["url"],
                    "title": row["title"],
                    "topic_id": row["topic_id"],
                    "status": row["status"],
                    "source": source,
                }
            )
            n += 1
        return n
    finally:
        db.close()


def run_locked(
    store: MongoStore,
    settings: Settings,
    kind: str,
    fn: Callable[[], int | dict[str, Any]],
) -> dict[str, Any]:
    if not _busy.acquire(blocking=False):
        return {"ok": False, "error": "another job is running", "busy": True}
    run_id = store.start_run(kind)
    _current["kind"] = kind
    _current["run_id"] = run_id
    store.write_log("info", kind, "started", run_id=run_id)
    try:
        raw = fn()
        if isinstance(raw, dict):
            code = int(raw.get("exit_code", 0 if raw.get("ok", True) else 1))
            payload = dict(raw)
        else:
            code = raw
            payload = {}
        ok = code == 0
        err = None if ok else f"exit_code={code}"
        store.finish_run(run_id, ok=ok, error=err)
        store.write_log(
            "info" if ok else "error",
            kind,
            "finished" if ok else err or "failed",
            run_id=run_id,
        )
        if not ok:
            emit_alert(
                store,
                settings,
                title=f"{kind} failed",
                message=err or "nonzero exit",
                run_id=run_id,
            )
        synced = 0
        if kind in {"discover", "inbox", "daily"}:
            synced = sync_links_from_queue(store, source=kind)
        out = {"ok": ok, "run_id": run_id, "exit_code": code, "links_synced": synced}
        out.update(payload)
        out["ok"] = ok
        out["run_id"] = run_id
        out["exit_code"] = code
        out["links_synced"] = synced
        return out
    except Exception as e:
        store.finish_run(run_id, ok=False, error=str(e))
        store.write_log("error", kind, str(e), run_id=run_id)
        emit_alert(
            store,
            settings,
            title=f"{kind} crashed",
            message=str(e),
            run_id=run_id,
        )
        return {"ok": False, "run_id": run_id, "error": str(e)}
    finally:
        _current["kind"] = None
        _current["run_id"] = None
        _busy.release()
