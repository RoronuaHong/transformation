from __future__ import annotations

from api import try_service as svc


def test_infer_try_progress_pending_shows_queued_when_another_job_runs(monkeypatch) -> None:
    monkeypatch.setattr(svc, "_busy", True)
    monkeypatch.setattr(svc, "_active_try_job_id", 5)
    out = svc.infer_try_progress("bilibili", "BVother", "pending", job_id=6)
    assert out["stage"] == "queued"
    assert out["percent"] == 0
    assert out["active"] is False


def test_infer_try_progress_processing_marks_stale_without_busy_mask(monkeypatch) -> None:
    monkeypatch.setattr(svc, "_busy", True)
    monkeypatch.setattr(svc, "_active_try_job_id", 5)
    monkeypatch.setattr(svc, "existing_wav", lambda _work: None)
    monkeypatch.setattr(svc, "list_locale_srts", lambda _work: {})
    monkeypatch.setattr(svc, "_latest_mtime", lambda _work: 1.0)
    monkeypatch.setattr("time.time", lambda: 1.0 + 400)
    out = svc.infer_try_progress("bilibili", "BVtest", "processing", job_id=5)
    assert out["stale"] is True
    assert out["active"] is False


def test_pause_resume_queue(monkeypatch) -> None:
    monkeypatch.setattr(svc, "_queue_paused", False)
    out = svc.pause_try_queue()
    assert out["ok"] is True
    assert out["paused"] is True
    out = svc.resume_try_queue()
    assert out["ok"] is True
    assert out["paused"] is False


def test_infer_try_progress_cancelled() -> None:
    out = svc.infer_try_progress("bilibili", "BVtest", "cancelled")
    assert out["stage"] == "cancelled"
    assert out["percent"] == 0
