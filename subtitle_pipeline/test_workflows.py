"""Tests for independent workflow expansion + queue uniqueness."""

from __future__ import annotations

from pathlib import Path

from discover.models import Candidate
from discover.queue_db import QueueDB
from workflows import expand_workflow_tasks, pack_id_for


def test_expand_single_translate() -> None:
    tasks = expand_workflow_tasks(want_translate=True)
    assert len(tasks) == 1
    assert tasks[0]["workflow"] == "translate"
    assert tasks[0]["stages"] == "fetch,asr,translate"


def test_expand_translate_then_notes_reuses_srt() -> None:
    tasks = expand_workflow_tasks(want_translate=True, want_notes=True)
    assert [t["workflow"] for t in tasks] == ["translate", "notes"]
    assert tasks[1]["stages"] == "notes,localize"


def test_expand_notes_only() -> None:
    tasks = expand_workflow_tasks(want_notes=True)
    assert len(tasks) == 1
    assert tasks[0]["workflow"] == "notes"
    assert tasks[0]["stages"] == "fetch,asr,notes,localize"


def test_expand_clips_independent_of_notes() -> None:
    tasks = expand_workflow_tasks(want_clips=True)
    assert len(tasks) == 1
    assert tasks[0]["workflow"] == "clips"
    assert tasks[0]["stages"] == "clips"
    assert tasks[0]["input_from"] == "source"


def test_expand_media_postproc_each_own_task() -> None:
    tasks = expand_workflow_tasks(
        want_dehardsub=True,
        want_deblur=True,
        want_enhance=True,
        want_compress=True,
    )
    assert [t["workflow"] for t in tasks] == [
        "dehardsub",
        "deblur",
        "enhance",
        "compress",
    ]


def test_expand_handoffs_notes_depends_translate() -> None:
    tasks = expand_workflow_tasks(want_translate=True, want_notes=True)
    notes = next(t for t in tasks if t["workflow"] == "notes")
    assert notes["depends_on"] == ["translate"]
    assert notes.get("input_from") is None


def test_expand_handoffs_enhance_input_from_dehardsub() -> None:
    tasks = expand_workflow_tasks(want_dehardsub=True, want_enhance=True)
    enh = next(t for t in tasks if t["workflow"] == "enhance")
    assert enh["input_from"] == "dehardsub"
    assert enh["depends_on"] == ["dehardsub"]


def test_expand_handoffs_override_input_from() -> None:
    tasks = expand_workflow_tasks(
        want_dehardsub=True,
        want_enhance=True,
        input_from={"enhance": "source"},
    )
    enh = next(t for t in tasks if t["workflow"] == "enhance")
    assert enh["input_from"] == "source"
    assert enh["depends_on"] == []


def test_expand_media_chain_compress_after_enhance() -> None:
    tasks = expand_workflow_tasks(want_enhance=True, want_compress=True)
    comp = next(t for t in tasks if t["workflow"] == "compress")
    assert comp["input_from"] == "enhance"
    assert comp["depends_on"] == ["enhance"]



def test_expand_legacy_stages_only() -> None:
    tasks = expand_workflow_tasks(stages="enhance,compress")
    assert len(tasks) == 1
    assert tasks[0]["stages"] == "enhance,compress"


def test_queue_allows_multiple_workflows_same_video(tmp_path: Path) -> None:
    db = QueueDB(tmp_path / "q.db")
    try:
        c = Candidate(
            platform="youtube",
            video_id="abc123",
            url="https://www.youtube.com/watch?v=abc123",
            title="t",
            topic_id="general",
        )
        pack = pack_id_for("youtube", "abc123")
        assert db.enqueue(c, workflow="translate", pack_id=pack) == "inserted"
        assert db.enqueue(c, workflow="notes", pack_id=pack) == "inserted"
        assert db.enqueue(c, workflow="enhance", pack_id=pack) == "inserted"
        rows = db.list_pack_jobs(pack)
        assert len(rows) == 3
        assert {r["workflow"] for r in rows} == {"translate", "notes", "enhance"}
        # Same workflow again while pending → ignored
        assert db.enqueue(c, workflow="translate", pack_id=pack) == "ignored"
    finally:
        db.close()


def test_queue_discover_all_still_unique(tmp_path: Path) -> None:
    db = QueueDB(tmp_path / "q2.db")
    try:
        c = Candidate(
            platform="bilibili",
            video_id="BVxxx",
            url="https://www.bilibili.com/video/BVxxx",
            title="t",
            topic_id="home_tips",
        )
        assert db.enqueue(c) == "inserted"  # workflow=all
        assert db.enqueue(c) == "ignored"
        # Independent WF can still be added beside discover all
        assert db.enqueue(c, workflow="compress") == "inserted"
    finally:
        db.close()


def test_workflow_home_and_products(tmp_path: Path) -> None:
    from workflows import workflow_home, workflow_products, infer_workflow_progress

    work = tmp_path / "pack"
    home = workflow_home(work, "enhance")
    assert home.name == "enhance"
    dest = home / "enhanced.mp4"
    dest.write_bytes(b"0" * 1000)
    products = workflow_products(work, "enhance")
    assert len(products) == 1
    assert products[0]["name"] == "enhanced.mp4"
    prog = infer_workflow_progress("enhance", "done", work)
    assert prog["percent"] == 100
    prog2 = infer_workflow_progress("enhance", "processing", work, active=True)
    assert prog2["percent"] >= 70


def test_write_pack_manifest(tmp_path: Path) -> None:
    from workflows import write_pack_manifest, workflow_home

    work = tmp_path / "pack2"
    (workflow_home(work, "compress") / "compressed.mp4").write_bytes(b"1" * 1000)
    path = write_pack_manifest(
        work,
        pack_id="youtube_x",
        tasks=[{"workflow": "compress", "job_id": 1, "status": "done"}],
    )
    assert path.is_file()
    import json

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["pack_id"] == "youtube_x"
    assert data["tasks"][0]["products"]


def test_claim_next_by_job_id(tmp_path: Path) -> None:
    db = QueueDB(tmp_path / "q3.db")
    try:
        c = Candidate(
            platform="youtube",
            video_id="vid",
            url="https://www.youtube.com/watch?v=vid",
            title="t",
            topic_id="general",
        )
        db.enqueue(c, workflow="translate", priority="high")
        db.enqueue(c, workflow="notes", priority="high")
        notes = db.get_job_by_workflow("youtube", "vid", "notes")
        assert notes is not None
        claimed = db.claim_next(max_attempts=5, job_id=int(notes["id"]))
        assert claimed is not None
        assert claimed["workflow"] == "notes"
        assert claimed["status"] == "processing"
    finally:
        db.close()
