"""Local Mongo collections: source links, logs, alerts, runs."""

from __future__ import annotations

import time
from typing import Any

from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.collection import Collection
from pymongo.database import Database

from api.settings import Settings


def utc_now() -> float:
    return time.time()


class MongoStore:
    """Ops store on a local mongod. Pipeline SQLite still drives ASR/batch."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = MongoClient(settings.mongo_uri, serverSelectionTimeoutMS=4000)
        self.db: Database = self.client[settings.mongo_db]
        self.links: Collection = self.db["source_links"]
        self.logs: Collection = self.db["logs"]
        self.alerts: Collection = self.db["alerts"]
        self.runs: Collection = self.db["runs"]
        self.ensure_indexes()

    def ping(self) -> None:
        self.client.admin.command("ping")

    def ensure_indexes(self) -> None:
        self.links.create_index(
            [("platform", ASCENDING), ("video_id", ASCENDING)],
            unique=True,
        )
        self.logs.create_index([("ts", DESCENDING)])
        self.alerts.create_index([("ts", DESCENDING)])
        self.alerts.create_index([("acked", ASCENDING)])
        self.runs.create_index([("started_at", DESCENDING)])

    def close(self) -> None:
        self.client.close()

    def upsert_source_link(self, doc: dict[str, Any]) -> None:
        platform = doc["platform"]
        video_id = doc["video_id"]
        now = utc_now()
        original = doc.get("original_url") or doc.get("canonical_url") or doc.get("url")
        canonical = doc.get("canonical_url") or doc.get("url") or original
        fields = {
            "original_url": original,
            "canonical_url": canonical,
            "url": canonical,
            "title": doc.get("title"),
            "author": doc.get("author"),
            "author_id": doc.get("author_id"),
            "published_at": doc.get("published_at"),
            "thumb_url": doc.get("thumb_url"),
            "views": doc.get("views"),
            "topic_id": doc.get("topic_id"),
            "status": doc.get("status"),
            "source": doc.get("source") or "pipeline",
        }
        # The queue is re-synced on every list request. Skip no-op writes so
        # "入库时间/顺序" stays stable instead of being refreshed every poll.
        current = self.links.find_one({"platform": platform, "video_id": video_id})
        if current is not None and all(current.get(k) == v for k, v in fields.items()):
            return
        self.links.update_one(
            {"platform": platform, "video_id": video_id},
            {"$set": {**fields, "updated_at": now}, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )

    def set_source_link_status(self, platform: str, video_id: str, status: str) -> bool:
        """Mirror a queue status change without a full queue → Mongo resync."""
        res = self.links.update_one(
            {"platform": platform, "video_id": video_id},
            {"$set": {"status": status, "updated_at": utc_now()}},
        )
        return res.matched_count == 1

    def delete_source_link(self, platform: str, video_id: str) -> bool:
        res = self.links.delete_one({"platform": platform, "video_id": video_id})
        return res.deleted_count == 1

    def list_links(self, *, limit: int = 100, platform: str | None = None) -> list[dict[str, Any]]:
        q: dict[str, Any] = {}
        if platform:
            q["platform"] = platform
        return [_oid(d) for d in self.links.find(q).sort("updated_at", DESCENDING).limit(limit)]

    def write_log(
        self,
        level: str,
        action: str,
        message: str,
        *,
        run_id: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.logs.insert_one(
            {
                "ts": utc_now(),
                "level": level,
                "action": action,
                "message": message,
                "run_id": run_id,
                "extra": extra or {},
            }
        )

    def list_logs(self, *, limit: int = 200) -> list[dict[str, Any]]:
        return [_oid(d) for d in self.logs.find().sort("ts", DESCENDING).limit(limit)]

    def raise_alert(
        self,
        title: str,
        message: str,
        *,
        severity: str = "error",
        run_id: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> str:
        doc = {
            "ts": utc_now(),
            "severity": severity,
            "title": title,
            "message": message,
            "run_id": run_id,
            "acked": False,
            "extra": extra or {},
        }
        res = self.alerts.insert_one(doc)
        return str(res.inserted_id)

    def list_alerts(self, *, limit: int = 100, unacked_only: bool = False) -> list[dict[str, Any]]:
        q: dict[str, Any] = {"acked": False} if unacked_only else {}
        return [_oid(d) for d in self.alerts.find(q).sort("ts", DESCENDING).limit(limit)]

    def ack_alert(self, alert_id: str) -> bool:
        from bson import ObjectId

        res = self.alerts.update_one(
            {"_id": ObjectId(alert_id)},
            {"$set": {"acked": True, "acked_at": utc_now()}},
        )
        return res.matched_count == 1

    def start_run(self, kind: str) -> str:
        res = self.runs.insert_one(
            {
                "kind": kind,
                "status": "running",
                "started_at": utc_now(),
                "finished_at": None,
                "error": None,
            }
        )
        return str(res.inserted_id)

    def finish_run(self, run_id: str, *, ok: bool, error: str | None = None) -> None:
        from bson import ObjectId

        self.runs.update_one(
            {"_id": ObjectId(run_id)},
            {
                "$set": {
                    "status": "ok" if ok else "failed",
                    "finished_at": utc_now(),
                    "error": error,
                }
            },
        )

    def list_runs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return [_oid(d) for d in self.runs.find().sort("started_at", DESCENDING).limit(limit)]


def _oid(doc: dict[str, Any]) -> dict[str, Any]:
    out = dict(doc)
    if "_id" in out:
        out["id"] = str(out.pop("_id"))
    return out
