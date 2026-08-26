"""In-memory stand-in used by unit tests (no mongod)."""

from __future__ import annotations

from api.mongo import MongoStore, utc_now
from api.settings import Settings


class MemoryStore(MongoStore):
    def __init__(self) -> None:
        self.settings = Settings(mongo_uri="mongodb://invalid", mongo_db="test")
        self.client = None  # type: ignore[assignment]
        self._links: dict[tuple[str, str], dict] = {}
        self._logs: list[dict] = []
        self._alerts: list[dict] = []
        self._runs: list[dict] = []
        self._ids = 0

    def ping(self) -> None:
        return None

    def ensure_indexes(self) -> None:
        return None

    def close(self) -> None:
        return None

    def upsert_source_link(self, doc: dict) -> None:
        key = (doc["platform"], doc["video_id"])
        original = doc.get("original_url") or doc.get("canonical_url") or doc.get("url")
        now = utc_now()
        prev = self._links.get(key) or {"created_at": now}
        prev.update(
            {
                "platform": doc["platform"],
                "video_id": doc["video_id"],
                "original_url": original,
                "canonical_url": doc.get("canonical_url") or original,
                "title": doc.get("title"),
                "topic_id": doc.get("topic_id"),
                "status": doc.get("status"),
                "source": doc.get("source") or "pipeline",
                "updated_at": now,
            }
        )
        self._links[key] = prev

    def list_links(self, *, limit: int = 100) -> list[dict]:
        rows = list(self._links.values())
        rows.sort(key=lambda d: d.get("updated_at") or 0, reverse=True)
        return rows[:limit]

    def write_log(self, level: str, action: str, message: str, *, run_id=None, extra=None) -> None:
        self._logs.append(
            {
                "id": str(len(self._logs) + 1),
                "ts": utc_now(),
                "level": level,
                "action": action,
                "message": message,
                "run_id": run_id,
                "extra": extra or {},
            }
        )

    def list_logs(self, *, limit: int = 200) -> list[dict]:
        return list(reversed(self._logs))[:limit]

    def raise_alert(self, title: str, message: str, *, severity="error", run_id=None, extra=None) -> str:
        self._ids += 1
        aid = str(self._ids)
        self._alerts.append(
            {
                "id": aid,
                "ts": utc_now(),
                "severity": severity,
                "title": title,
                "message": message,
                "run_id": run_id,
                "acked": False,
                "extra": extra or {},
            }
        )
        return aid

    def list_alerts(self, *, limit: int = 100, unacked_only: bool = False) -> list[dict]:
        rows = [a for a in self._alerts if (not unacked_only or not a["acked"])]
        return list(reversed(rows))[:limit]

    def ack_alert(self, alert_id: str) -> bool:
        for a in self._alerts:
            if a["id"] == alert_id:
                a["acked"] = True
                return True
        return False

    def start_run(self, kind: str) -> str:
        self._ids += 1
        rid = str(self._ids)
        self._runs.append(
            {
                "id": rid,
                "kind": kind,
                "status": "running",
                "started_at": utc_now(),
                "finished_at": None,
                "error": None,
            }
        )
        return rid

    def finish_run(self, run_id: str, *, ok: bool, error: str | None = None) -> None:
        for r in self._runs:
            if r["id"] == run_id:
                r["status"] = "ok" if ok else "failed"
                r["finished_at"] = utc_now()
                r["error"] = error

    def list_runs(self, *, limit: int = 50) -> list[dict]:
        return list(reversed(self._runs))[:limit]
