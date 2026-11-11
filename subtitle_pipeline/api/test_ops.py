from __future__ import annotations

from pathlib import Path

from api.memory_store import MemoryStore
from api.runner import run_locked
from api.security import emit_alert
from api.settings import Settings
from discover.models import Candidate
from discover.queue_db import QueueDB


def test_enqueue_keeps_original_url(tmp_path: Path) -> None:
    db = QueueDB(tmp_path / "q.db")
    try:
        raw = "https://youtu.be/kV7RuutRx-s"
        c = Candidate(
            platform="youtube",
            video_id="kV7RuutRx-s",
            url="https://www.youtube.com/watch?v=kV7RuutRx-s",
            original_url=raw,
            title="t",
            topic_id="ai_monetize",
        )
        assert db.enqueue(c) == "inserted"
        row = db.list_jobs(limit=1)[0]
        assert row["original_url"] == raw
        assert row["canonical_url"].endswith("kV7RuutRx-s")
    finally:
        db.close()


def test_alert_and_ack() -> None:
    store = MemoryStore()
    settings = Settings(alert_webhook_url="")
    aid = emit_alert(store, settings, title="batch failed", message="403")
    assert store.list_alerts(unacked_only=True)[0]["id"] == aid
    assert store.ack_alert(aid) is True
    assert store.list_alerts(unacked_only=True) == []


def test_run_locked_keeps_summary_payload() -> None:
    store = MemoryStore()
    settings = Settings(alert_webhook_url="")
    out = run_locked(
        store,
        settings,
        "game_claim",
        lambda: {"ok": True, "exit_code": 0, "summary_path": "runs/summary.json", "claimed": 2},
    )
    assert out["ok"] is True
    assert out["summary_path"] == "runs/summary.json"
    assert out["claimed"] == 2


def test_video_level_queue_controls(tmp_path: Path) -> None:
    db = QueueDB(tmp_path / "q.db")
    try:
        for vid in ("v1", "v2"):
            db.enqueue(
                Candidate(
                    platform="youtube",
                    video_id=vid,
                    url=f"https://youtu.be/{vid}",
                    title=vid,
                    topic_id="crawler:manual",
                )
            )
        assert len(db.list_video_jobs("youtube", "v1")) == 1
        assert db.delete_video_jobs("youtube", "v1") == 1
        assert db.list_video_jobs("youtube", "v1") == []
        assert len(db.list_video_jobs("youtube", "v2")) == 1
        assert db.delete_video_jobs("youtube", "missing") == 0
    finally:
        db.close()


def test_crawler_links_action_dual_write(tmp_path: Path, monkeypatch) -> None:
    """Drawer actions must update SQLite jobs AND the Mongo mirror together."""
    from api import app as app_module
    from api.runner import sync_links_from_queue

    monkeypatch.setattr("discover.queue_db.DEFAULT_DB", tmp_path / "queue.db")
    mem = MemoryStore()
    monkeypatch.setattr(app_module, "_store", mem)

    for platform, vid in (("youtube", "v1"), ("bilibili", "v2")):
        db = QueueDB()
        try:
            assert db.enqueue(
                Candidate(
                    platform=platform,
                    video_id=vid,
                    url=f"https://example.com/{vid}",
                    title=vid,
                    topic_id="crawler:manual",
                ),
                priority="high",
            ) == "inserted"
        finally:
            db.close()
    sync_links_from_queue(mem, source="test")
    assert {d["video_id"] for d in mem.list_links()} == {"v1", "v2"}

    refs = [
        {"platform": "youtube", "video_id": "v1"},
        {"platform": "bilibili", "video_id": "v2"},
    ]
    both = {"youtube:v1", "bilibili:v2"}

    def act(action: str, items: list[dict[str, str]] | None = None) -> dict:
        return app_module.admin_crawler_links_action(
            app_module.CrawlerLinkActionBody(action=action, items=items or refs, limit=30)
        )

    out = act("pause")
    assert set(out["succeeded"]) == both
    assert {d["status"] for d in mem.list_links()} == {"cancelled"}
    assert all(d["status"] == "cancelled" for d in out["items"])

    # A second pause is a no-op, reported as skipped instead of an error.
    assert len(act("pause")["skipped"]) == 2

    out = act("resume")
    assert set(out["succeeded"]) == both
    assert {d["status"] for d in mem.list_links()} == {"pending"}
    assert len(act("reprocess")["skipped"]) == 2

    # Reprocess only applies to finished jobs.
    db = QueueDB()
    try:
        row = db.list_video_jobs("youtube", "v1")[0]
        db.mark_done(int(row["id"]))
    finally:
        db.close()
    sync_links_from_queue(mem, source="test")
    out = act("reprocess", [refs[0]])
    assert out["succeeded"] == ["youtube:v1"]
    assert next(d for d in mem.list_links() if d["video_id"] == "v1")["status"] == "pending"

    # Delete removes both stores, so the next sync cannot resurrect the item.
    out = act("delete")
    assert set(out["succeeded"]) == both
    assert mem.list_links() == []
    assert out["items"] == []
    sync_links_from_queue(mem, source="test")
    assert mem.list_links() == []
    db = QueueDB()
    try:
        assert db.list_video_jobs("youtube", "v1") == []
        assert db.list_video_jobs("bilibili", "v2") == []
    finally:
        db.close()

    assert len(act("delete")["skipped"]) == 2
