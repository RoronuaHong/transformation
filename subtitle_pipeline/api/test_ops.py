from __future__ import annotations

from pathlib import Path

from api.memory_store import MemoryStore
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
