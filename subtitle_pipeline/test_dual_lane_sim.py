"""Instance simulation: dual-lane expand / deps / slots (no GPU/Ollama)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from discover.models import Candidate
from discover.queue_db import QueueDB
from workflows import (
    ARTIFACT_SOURCE_SRT,
    expand_workflow_tasks,
    lane_for_workflow,
    pack_id_for,
)


def test_sim_full_pack_expand_lanes_and_fanout() -> None:
    tasks = expand_workflow_tasks(
        want_translate=True,
        want_notes=True,
        want_dehardsub=True,
        want_clips=True,
        want_concat=True,
        want_deblur=True,
        want_enhance=True,
        want_compress=True,
        want_remix=True,
    )
    by = {t["workflow"]: t for t in tasks}
    assert by["translate"]["lane"] == "content"
    assert by["notes"]["lane"] == "content"
    assert by["notes"]["depends_on"] == []
    assert by["notes"]["depends_on_artifacts"] == [ARTIFACT_SOURCE_SRT]
    assert by["dehardsub"]["lane"] == "media"
    assert by["deblur"]["depends_on"] == ["dehardsub"]
    assert "concat" not in by["deblur"]["depends_on"]
    assert by["concat"]["depends_on"] == ["clips"]
    # Content can start with media in parallel: dehardsub has no content deps.
    assert by["dehardsub"]["depends_on"] == []


def test_sim_deps_source_srt_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from api import try_service as ts

    platform, vid = "bilibili", "BV_SIM_DEPS"
    work = tmp_path / f"{platform}_{vid}"
    (work / "subs").mkdir(parents=True)
    monkeypatch.setattr(ts, "_work_dir", lambda p, v: work)

    db = QueueDB(tmp_path / "q.db")
    try:
        c = Candidate(
            platform=platform,
            video_id=vid,
            url=f"https://www.bilibili.com/video/{vid}",
            title="sim",
            topic_id="general",
        )
        pack = pack_id_for(platform, vid)
        assert db.enqueue(c, workflow="translate", pack_id=pack, priority="high") == "inserted"
        assert db.enqueue(c, workflow="notes", pack_id=pack, priority="high") == "inserted"
        notes = db.get_job_by_workflow(platform, vid, "notes")
        assert notes is not None
        db.set_meta(
            int(notes["id"]),
            {
                "workflow": "notes",
                "lane": "content",
                "depends_on": [],
                "depends_on_artifacts": [ARTIFACT_SOURCE_SRT],
                "stages": "notes,localize",
            },
        )
        notes = db.get_job_by_workflow(platform, vid, "notes")
        assert ts._deps_satisfied(db, notes) is False

        # Bare zh.srt without sync_meta must not unlock notes.
        zh = work / "subs" / "zh.srt"
        zh.write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n", encoding="utf-8")
        assert ts._deps_satisfied(db, notes) is False

        srt = work / "subs" / "source.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n", encoding="utf-8")
        assert ts._deps_satisfied(db, notes) is True

        # Target-only SRT must not unlock notes.
        srt.unlink()
        zh.unlink()
        (work / "subs" / "fr.srt").write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nbonjour\n", encoding="utf-8"
        )
        assert ts._deps_satisfied(db, notes) is False
    finally:
        db.close()


def test_sim_slot_caps_content_and_media(monkeypatch: pytest.MonkeyPatch) -> None:
    from api import try_service as ts

    monkeypatch.setenv("VITUAL_MAX_ACTIVE_JOBS", "3")
    monkeypatch.setenv("VITUAL_CONTENT_LANE_CAP", "2")
    monkeypatch.setenv("VITUAL_MEDIA_LANE_CAP", "1")
    # Reset in-memory slots.
    with ts._run_lock:
        ts._active_jobs["content"].clear()
        ts._active_jobs["media"].clear()

    assert ts._try_acquire_slot("content", 1) is True
    assert ts._try_acquire_slot("content", 1) is False  # already leased
    assert ts._try_acquire_slot("media", 2) is True  # parallel lane
    assert ts._try_acquire_slot("content", 3) is True  # 2nd content (notes)
    assert ts._try_acquire_slot("media", 4) is False  # media cap 1
    assert ts._try_acquire_slot("content", 5) is False  # content cap 2 / total 3

    ts._release_slot("content", 1)
    ts._release_slot("media", 2)
    ts._release_slot("content", 3)

    monkeypatch.setenv("VITUAL_MAX_ACTIVE_JOBS", "1")
    with ts._run_lock:
        ts._active_jobs["content"].clear()
        ts._active_jobs["media"].clear()
    assert ts._try_acquire_slot("content", 10) is True
    assert ts._try_acquire_slot("media", 11) is False  # serial fallback


def test_sim_scheduler_picks_media_beside_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dry-run the claim selection the scheduler uses (no process_job)."""
    from api import try_service as ts

    monkeypatch.setenv("VITUAL_MAX_ACTIVE_JOBS", "3")
    monkeypatch.setenv("VITUAL_CONTENT_LANE_CAP", "2")
    monkeypatch.setenv("VITUAL_MEDIA_LANE_CAP", "1")
    with ts._run_lock:
        ts._active_jobs["content"].clear()
        ts._active_jobs["media"].clear()

    db = QueueDB(tmp_path / "q2.db")
    try:
        c = Candidate(
            platform="bilibili",
            video_id="BV_SIM_SCHED",
            url="https://www.bilibili.com/video/BV_SIM_SCHED",
            title="sim",
            topic_id="general",
        )
        pack = pack_id_for("bilibili", "BV_SIM_SCHED")
        specs = expand_workflow_tasks(
            want_translate=True,
            want_notes=True,
            want_dehardsub=True,
            want_deblur=True,
        )
        ids: dict[str, int] = {}
        for spec in specs:
            assert db.enqueue(c, workflow=spec["workflow"], pack_id=pack, priority="high")
            row = db.get_job_by_workflow("bilibili", "BV_SIM_SCHED", spec["workflow"])
            assert row is not None
            jid = int(row["id"])
            ids[spec["workflow"]] = jid
            meta = {
                "workflow": spec["workflow"],
                "lane": spec.get("lane") or lane_for_workflow(spec["workflow"]),
                "stages": spec["stages"],
                "depends_on": list(spec.get("depends_on") or []),
            }
            if spec.get("depends_on_artifacts"):
                meta["depends_on_artifacts"] = list(spec["depends_on_artifacts"])
            db.set_meta(jid, meta)

        # Simulate one scheduler wake: acquire runnable jobs in created_at order.
        picked: list[tuple[str, str]] = []
        rows = db._conn.execute(
            """
            SELECT id FROM jobs
            WHERE status='pending' AND priority='high'
            ORDER BY created_at ASC
            """
        ).fetchall()
        for row in rows:
            jid = int(row["id"])
            job = db.get_job(jid)
            assert job is not None
            if not ts._deps_satisfied(db, job):
                continue
            lane = ts._lane_for_job(job)
            if not ts._try_acquire_slot(lane, jid):
                continue
            picked.append((lane, str(job["workflow"])))

        # Expect content translate + media dehardsub in the first wake
        # (notes still blocked without source_srt).
        assert ("content", "translate") in picked
        assert ("media", "dehardsub") in picked
        assert ("content", "notes") not in picked
        assert ("media", "deblur") not in picked  # waits dehardsub done

        for lane, _wf in picked:
            # release by scanning active lists
            pass
        with ts._run_lock:
            ts._active_jobs["content"].clear()
            ts._active_jobs["media"].clear()
    finally:
        db.close()


def test_upsert_maps_processing_to_already_processing(tmp_path: Path) -> None:
    from api import try_service as ts
    from discover.models import Candidate

    db = QueueDB(tmp_path / "proc.db")
    try:
        c = Candidate(
            platform="bilibili",
            video_id="BV_PROC",
            url="https://www.bilibili.com/video/BV_PROC",
            title="proc",
            topic_id="general",
        )
        assert db.enqueue(c, workflow="translate", pack_id="bilibili_BV_PROC", priority="high")
        jid = int(db.get_job_by_workflow("bilibili", "BV_PROC", "translate")["id"])
        db._conn.execute(
            "UPDATE jobs SET status='processing', updated_at=? WHERE id=?",
            (0, jid),
        )
        db._conn.commit()
        out_id, enq = ts._upsert_workflow_job(
            db, c, workflow="translate", pack_id="bilibili_BV_PROC"
        )
        assert out_id == jid
        assert enq == "already_processing"
    finally:
        db.close()


def test_pack_has_source_srt_ignores_target_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api import try_service as ts

    work = tmp_path / "bilibili_BV_SRC"
    (work / "subs").mkdir(parents=True)
    (work / "media").mkdir(parents=True)
    monkeypatch.setattr(ts, "_work_dir", lambda p, v: work)
    (work / "subs" / "fr.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nx\n", encoding="utf-8"
    )
    assert ts._pack_has_source_srt("bilibili", "BV_SRC") is False
    # Bare zh.srt without sync_meta/notes must NOT unlock notes (could be leftover).
    (work / "subs" / "zh.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nhi\n", encoding="utf-8"
    )
    assert ts._pack_has_source_srt("bilibili", "BV_SRC") is False
    (work / "media" / "sync_meta.json").write_text(
        '{"source_lang":"zh"}', encoding="utf-8"
    )
    assert ts._pack_has_source_srt("bilibili", "BV_SRC") is True
    (work / "media" / "sync_meta.json").unlink()
    assert ts._pack_has_source_srt("bilibili", "BV_SRC") is False
    (work / "subs" / "source.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nsrc\n", encoding="utf-8"
    )
    assert ts._pack_has_source_srt("bilibili", "BV_SRC") is True


def test_kick_try_scheduler_respects_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    from api import try_service as ts

    monkeypatch.setattr(ts, "_queue_paused", True)
    called = {"n": 0}

    def _fake_schedule(*, skip_job_id=None):  # noqa: ANN001
        called["n"] += 1

    # kick always calls schedule_next; schedule's runner no-ops when paused.
    monkeypatch.setattr(ts, "schedule_next_try_job", _fake_schedule)
    out = ts.kick_try_scheduler()
    assert called["n"] == 1
    assert "active_job_ids" in out


def test_cancel_fans_out_to_pack_siblings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api import try_service as ts

    monkeypatch.setattr(ts, "QueueDB", lambda: QueueDB(tmp_path / "cancel.db"))
    monkeypatch.setattr(ts, "kick_try_scheduler", lambda: {"ok": True})
    monkeypatch.setattr(
        ts,
        "job_snapshot",
        lambda jid: {"ok": True, "job_id": jid, "status": "cancelled"},
    )

    db = QueueDB(tmp_path / "cancel.db")
    try:
        c = Candidate(
            platform="bilibili",
            video_id="BV_CANCEL_PACK",
            url="https://www.bilibili.com/video/BV_CANCEL_PACK",
            title="sim",
            topic_id="general",
        )
        pack = pack_id_for("bilibili", "BV_CANCEL_PACK")
        for wf in ("translate", "notes", "dehardsub"):
            assert db.enqueue(c, workflow=wf, pack_id=pack, priority="high") == "inserted"
        rows = db.list_video_jobs("bilibili", "BV_CANCEL_PACK")
        ids = [int(r["id"]) for r in rows]
        assert len(ids) == 3
        primary = ids[0]
        out = ts.cancel_try_job(primary)
        assert out.get("ok") is True
        assert set(out.get("cancelled_job_ids") or []) == set(ids)
        for jid in ids:
            row = db.get_job(jid)
            assert row is not None
            assert row["status"] == "cancelled"
    finally:
        db.close()


def test_upsert_blocks_requeue_while_slot_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api import try_service as ts
    from discover.models import Candidate

    db = QueueDB(tmp_path / "zombie.db")
    try:
        c = Candidate(
            platform="bilibili",
            video_id="BV_ZOMBIE",
            url="https://www.bilibili.com/video/BV_ZOMBIE",
            title="zombie",
            topic_id="general",
        )
        assert db.enqueue(c, workflow="translate", pack_id="p", priority="high")
        jid = int(db.get_job_by_workflow("bilibili", "BV_ZOMBIE", "translate")["id"])
        assert db.cancel_job(jid)
        with ts._run_lock:
            ts._active_jobs["content"].clear()
            ts._active_jobs["media"].clear()
            ts._active_jobs["content"].append(jid)
        ts._cancel_requested.add(jid)
        out_id, enq = ts._upsert_workflow_job(
            db, c, workflow="translate", pack_id="p"
        )
        assert out_id == jid
        assert enq == "already_processing"
        assert db.get_job(jid)["status"] == "cancelled"
        assert jid in ts._cancel_requested
    finally:
        with ts._run_lock:
            ts._active_jobs["content"].clear()
        ts._cancel_requested.discard(jid)
        db.close()


def test_retry_fans_out_to_cancelled_siblings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api import try_service as ts

    monkeypatch.setattr(ts, "QueueDB", lambda: QueueDB(tmp_path / "retry.db"))
    monkeypatch.setattr(ts, "kick_try_scheduler", lambda: {"ok": True})
    monkeypatch.setattr(
        ts,
        "job_snapshot",
        lambda jid: {"ok": True, "job_id": jid, "status": "pending"},
    )

    db = QueueDB(tmp_path / "retry.db")
    try:
        c = Candidate(
            platform="bilibili",
            video_id="BV_RETRY_PACK",
            url="https://www.bilibili.com/video/BV_RETRY_PACK",
            title="sim",
            topic_id="general",
        )
        pack = pack_id_for("bilibili", "BV_RETRY_PACK")
        for wf in ("translate", "notes", "dehardsub"):
            assert db.enqueue(c, workflow=wf, pack_id=pack, priority="high") == "inserted"
        rows = db.list_video_jobs("bilibili", "BV_RETRY_PACK")
        for r in rows:
            assert db.cancel_job(int(r["id"]))
        primary = int(rows[0]["id"])
        out = ts.retry_try_job(primary)
        assert out.get("ok") is True
        assert set(out.get("requeued_job_ids") or []) == {int(r["id"]) for r in rows}
        for r in rows:
            assert db.get_job(int(r["id"]))["status"] == "pending"
    finally:
        db.close()


def test_enqueue_meta_atomic_with_insert(tmp_path: Path) -> None:
    db = QueueDB(tmp_path / "meta.db")
    try:
        c = Candidate(
            platform="bilibili",
            video_id="BV_META",
            url="https://www.bilibili.com/video/BV_META",
            title="meta",
            topic_id="general",
        )
        assert (
            db.enqueue(
                c,
                workflow="notes",
                pack_id="p",
                priority="high",
                meta={
                    "depends_on_artifacts": [ARTIFACT_SOURCE_SRT],
                    "lane": "content",
                    "stages": "notes",
                },
            )
            == "inserted"
        )
        row = db.get_job_by_workflow("bilibili", "BV_META", "notes")
        meta = json.loads(row["meta_json"])
        assert meta["depends_on_artifacts"] == [ARTIFACT_SOURCE_SRT]
        assert meta["lane"] == "content"
    finally:
        db.close()


def test_apply_cancel_discards_sticky_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api import try_service as ts

    db = QueueDB(tmp_path / "sticky.db")
    try:
        c = Candidate(
            platform="bilibili",
            video_id="BV_STICKY",
            url="https://www.bilibili.com/video/BV_STICKY",
            title="sticky",
            topic_id="general",
        )
        assert db.enqueue(c, workflow="translate", pack_id="p", priority="high")
        jid = int(db.get_job_by_workflow("bilibili", "BV_STICKY", "translate")["id"])
        db._conn.execute(
            "UPDATE jobs SET status='processing', updated_at=? WHERE id=?",
            (0, jid),
        )
        db._conn.commit()
        ts._cancel_requested.add(jid)
        ts._apply_cancel(db, jid)
        assert jid not in ts._cancel_requested
        assert db.get_job(jid)["status"] == "cancelled"
    finally:
        ts._cancel_requested.discard(jid)
        db.close()


def test_reap_skips_only_active_slot_holders(monkeypatch: pytest.MonkeyPatch) -> None:
    from api import try_service as ts

    monkeypatch.setattr(ts, "_busy_flag", lambda: True)  # legacy would bail
    with ts._run_lock:
        ts._active_jobs["content"].clear()
        ts._active_jobs["media"].clear()
        ts._active_jobs["content"].append(101)

    # Active holder must be skipped even if we would reap.
    assert ts._is_active_job(101) is True
    assert ts._is_active_job(999) is False
    with ts._run_lock:
        ts._active_jobs["content"].clear()


def test_group_delete_handles_sqlite_row_and_cascades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Delete must not call .get() on sqlite3.Row; dependents cascade away."""
    from api import try_service as ts

    db_path = tmp_path / "group_del.db"
    monkeypatch.setattr(ts, "QueueDB", lambda: QueueDB(db_path))
    monkeypatch.setattr(ts, "kick_try_scheduler", lambda: {"ok": True})

    db = QueueDB(db_path)
    try:
        c = Candidate(
            platform="bilibili",
            video_id="BV_GROUP_DEL",
            url="https://www.bilibili.com/video/BV_GROUP_DEL",
            title="group-del",
            topic_id="general",
        )
        pack = pack_id_for("bilibili", "BV_GROUP_DEL")
        assert db.enqueue(c, workflow="dehardsub", pack_id=pack, priority="high") == "inserted"
        assert (
            db.enqueue(
                c,
                workflow="deblur",
                pack_id=pack,
                priority="high",
                meta={"depends_on": ["dehardsub"]},
            )
            == "inserted"
        )
        rows = db.list_video_jobs("bilibili", "BV_GROUP_DEL")
        by_wf = {str(r["workflow"]): int(r["id"]) for r in rows}
        for jid in by_wf.values():
            assert db.cancel_job(jid)

        out = ts.group_action_try_jobs([by_wf["dehardsub"]], "delete")
        assert out.get("ok") is True
        assert set(out.get("job_ids") or []) == set(by_wf.values())
        assert db.get_job(by_wf["dehardsub"]) is None
        assert db.get_job(by_wf["deblur"]) is None
    finally:
        db.close()


def test_group_delete_rejects_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api import try_service as ts

    db_path = tmp_path / "group_del_active.db"
    monkeypatch.setattr(ts, "QueueDB", lambda: QueueDB(db_path))

    db = QueueDB(db_path)
    try:
        c = Candidate(
            platform="bilibili",
            video_id="BV_GROUP_DEL_ACTIVE",
            url="https://www.bilibili.com/video/BV_GROUP_DEL_ACTIVE",
            title="active",
            topic_id="general",
        )
        assert db.enqueue(c, workflow="notes", pack_id="p", priority="high") == "inserted"
        jid = int(db.get_job_by_workflow("bilibili", "BV_GROUP_DEL_ACTIVE", "notes")["id"])
        out = ts.group_action_try_jobs([jid], "delete")
        assert out.get("ok") is False
        assert "进行中" in str(out.get("error") or "")
        assert db.get_job(jid) is not None
    finally:
        db.close()
