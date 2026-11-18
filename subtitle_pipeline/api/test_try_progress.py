from __future__ import annotations

from api import try_service as svc


def test_infer_try_progress_pending_shows_queued_when_another_job_runs(monkeypatch) -> None:
    # 444968a 起 pending 走 schedule-aware hint：本 job 未激活、且查库失败时
    # 若 _busy_flag() 为真 → queued（另一条 lane 在跑）。
    monkeypatch.setattr(svc, "_is_active_job", lambda _jid: False)
    monkeypatch.setattr(svc, "_busy_flag", lambda: True)

    def _boom():
        raise RuntimeError("no queue db in unit test")

    monkeypatch.setattr(svc, "QueueDB", _boom)
    out = svc.infer_try_progress("bilibili", "BVother", "pending", job_id=6)
    assert out["stage"] == "queued"
    assert out["percent"] == 0
    assert out["active"] is False


def test_infer_try_progress_processing_marks_stale_without_busy_mask(monkeypatch) -> None:
    # stale 由 _latest_mtime + time 判定，不再依赖 _busy 掩码。
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
