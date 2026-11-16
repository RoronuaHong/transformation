"""P2 job_steps: aggregate / resume skip / requeue."""

from __future__ import annotations

from pathlib import Path

from discover.models import Candidate
from discover.queue_db import QueueDB


def test_aggregate_partial_and_done(tmp_path: Path) -> None:
    db = QueueDB(tmp_path / "steps.db")
    try:
        c = Candidate(
            platform="bilibili",
            video_id="BV_STEPS",
            url="https://www.bilibili.com/video/BV_STEPS",
            title="steps",
            topic_id="general",
        )
        assert db.enqueue(c, workflow="translate", priority="high") == "inserted"
        job = db.get_job_by_workflow("bilibili", "BV_STEPS", "translate")
        assert job is not None
        jid = int(job["id"])

        db.upsert_step(jid, "translate.fr", kind="translate", lang="fr", status="queued")
        db.upsert_step(jid, "translate.de", kind="translate", lang="de", status="queued")
        db.mark_step_done(jid, "translate.fr", qa_status="ok", artifact_rel="subs/fr.srt")
        db.mark_step_failed(jid, "translate.de", "boom")

        assert db.aggregate_job_from_steps(jid) == "partial"
        assert db.get_job(jid)["status"] == "partial"
        summary = db.steps_summary(jid)
        assert summary["done"] == 1
        assert summary["total"] == 2
        assert "translate.de" in summary["failed_keys"]

        db.mark_step_done(jid, "translate.de", qa_status="ok", artifact_rel="subs/de.srt")
        assert db.aggregate_job_from_steps(jid) == "done"
        assert db.get_job(jid)["status"] == "done"
    finally:
        db.close()


def test_aggregate_all_failed(tmp_path: Path) -> None:
    db = QueueDB(tmp_path / "fail.db")
    try:
        c = Candidate(
            platform="bilibili",
            video_id="BV_FAIL",
            url="https://www.bilibili.com/video/BV_FAIL",
            title="fail",
            topic_id="general",
        )
        assert db.enqueue(c, workflow="translate", priority="high") == "inserted"
        jid = int(db.get_job_by_workflow("bilibili", "BV_FAIL", "translate")["id"])
        db.upsert_step(jid, "translate.fr", kind="translate", lang="fr")
        db.upsert_step(jid, "translate.de", kind="translate", lang="de")
        db.mark_step_failed(jid, "translate.fr", "a")
        db.mark_step_failed(jid, "translate.de", "b")
        assert db.aggregate_job_from_steps(jid) == "failed"
        assert db.get_job(jid)["status"] == "failed"
    finally:
        db.close()


def test_requeue_step_bumps_attempts(tmp_path: Path) -> None:
    db = QueueDB(tmp_path / "rq.db")
    try:
        c = Candidate(
            platform="upload",
            video_id="u1",
            url="file://u1",
            title="u",
            topic_id="general",
        )
        db.enqueue(c, workflow="translate", priority="high")
        jid = int(db.get_job_by_workflow("upload", "u1", "translate")["id"])
        db.upsert_step(jid, "translate.fr", kind="translate", lang="fr", status="failed")
        db.mark_step_failed(jid, "translate.fr", "x")
        assert db.requeue_step(jid, "translate.fr") is True
        row = db.get_step(jid, "translate.fr")
        assert row is not None
        assert row["status"] == "queued"
        assert int(row["attempts"]) == 1
    finally:
        db.close()


def test_needs_review_keeps_parent_done(tmp_path: Path) -> None:
    db = QueueDB(tmp_path / "qa.db")
    try:
        c = Candidate(
            platform="bilibili",
            video_id="BV_QA",
            url="https://www.bilibili.com/video/BV_QA",
            title="qa",
            topic_id="general",
        )
        db.enqueue(c, workflow="translate", priority="high")
        jid = int(db.get_job_by_workflow("bilibili", "BV_QA", "translate")["id"])
        db.upsert_step(jid, "translate.fr", kind="translate", lang="fr")
        db.mark_step_done(
            jid, "translate.fr", qa_status="needs_review", artifact_rel="subs/fr.srt"
        )
        assert db.aggregate_job_from_steps(jid) == "done"
        assert "translate.fr" in db.steps_summary(jid)["review_keys"]
    finally:
        db.close()


def test_reset_running_steps(tmp_path: Path) -> None:
    db = QueueDB(tmp_path / "reap.db")
    try:
        c = Candidate(
            platform="bilibili",
            video_id="BV_REAP",
            url="https://www.bilibili.com/video/BV_REAP",
            title="reap",
            topic_id="general",
        )
        db.enqueue(c, workflow="translate", priority="high")
        jid = int(db.get_job_by_workflow("bilibili", "BV_REAP", "translate")["id"])
        db.upsert_step(jid, "translate.fr", kind="translate", lang="fr", status="running")
        db.mark_step_running(jid, "translate.fr")
        assert db.reset_running_steps_to_queued() == 1
        assert db.get_step(jid, "translate.fr")["status"] == "queued"
    finally:
        db.close()


def test_requeue_job_clears_steps(tmp_path: Path) -> None:
    db = QueueDB(tmp_path / "clear.db")
    try:
        c = Candidate(
            platform="bilibili",
            video_id="BV_CLR",
            url="https://www.bilibili.com/video/BV_CLR",
            title="clr",
            topic_id="general",
        )
        db.enqueue(c, workflow="translate", priority="high")
        jid = int(db.get_job_by_workflow("bilibili", "BV_CLR", "translate")["id"])
        db.upsert_step(jid, "translate.fr", kind="translate", lang="fr")
        db.mark_partial(jid, error="partial")
        assert db.requeue_job(jid) is True
        assert db.list_steps(jid) == []
        assert db.get_job(jid)["status"] == "pending"
    finally:
        db.close()


def test_cancelled_job_blocks_step_mutations(tmp_path: Path) -> None:
    db = QueueDB(tmp_path / "cancel_steps.db")
    try:
        c = Candidate(
            platform="bilibili",
            video_id="BV_CXL",
            url="https://www.bilibili.com/video/BV_CXL",
            title="cxl",
            topic_id="general",
        )
        db.enqueue(c, workflow="translate", priority="high")
        jid = int(db.get_job_by_workflow("bilibili", "BV_CXL", "translate")["id"])
        db.upsert_step(jid, "translate.fr", kind="translate", lang="fr", status="running")
        assert db.cancel_job(jid, reason="用户取消") is True
        assert db.get_step(jid, "translate.fr")["status"] == "failed"
        db.mark_step_done(jid, "translate.fr", qa_status="ok", artifact_rel="subs/fr.srt")
        db.mark_step_failed(jid, "translate.fr", "should-not-overwrite")
        row = db.get_step(jid, "translate.fr")
        assert row["status"] == "failed"
        assert "用户取消" in str(row["error"] or "")
        assert db.aggregate_job_from_steps(jid) == "cancelled"
        assert db.get_job(jid)["status"] == "cancelled"
    finally:
        db.close()
