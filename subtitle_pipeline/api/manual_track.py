"""Register manually-launched CLI tasks in the try jobs queue.

Some heavy jobs (dehardsub_workflow.py confirm, ``python -c run_postproc``)
are launched directly from a shell, bypassing /api/try/*. They then never
appear in the workbench progress/history lists. This helper writes a
``priority=normal, status=processing`` job to ``data/queue.db`` so they show up.

Why priority=normal + status=processing:
  * try scheduler only claims priority='high' pending jobs  -> won't double-run
  * batch cron claim_next only takes status='pending'       -> won't double-run
  * stale reaper only scans priority='high' processing jobs -> won't mark failed

Work dir is keyed by <platform>_<video_id> exactly like /api/try jobs, so
progress is inferred from the on-disk products the script already writes.

Usage (run with subtitle_pipeline/.venv python):
  python api/manual_track.py start <work_dir> --workflow dehardsub [--title T] [--platform bilibili]
  python api/manual_track.py done <job_id>
  python api/manual_track.py fail <job_id> [--error MSG]
  python api/manual_track.py status
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from discover.models import Candidate  # noqa: E402
from discover.queue_db import QueueDB  # noqa: E402

MARKER = ".manual_job_id"


def _platform_video(work_dir: Path, platform: str | None) -> tuple[str, str]:
    """Derive (platform, video_id) from a work dir named '<platform>_<id>'."""
    name = work_dir.name
    if platform:
        return platform, name
    if "_" in name:
        head, tail = name.split("_", 1)
        if head in ("bilibili", "youtube", "douyin", "upload", "hls"):
            return head, tail
    return "manual", name


def cmd_start(args: argparse.Namespace) -> int:
    work = Path(args.work_dir).expanduser().resolve()
    if not work.is_dir():
        print(f"work dir not found: {work}", file=sys.stderr)
        return 2
    platform, video_id = _platform_video(work, args.platform)
    workflow = args.workflow or "dehardsub"
    title = args.title or video_id

    marker = work / MARKER
    if marker.is_file():
        try:
            old_id = int(marker.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            old_id = 0
        if old_id:
            q = QueueDB()
            try:
                job = q.get_job(old_id)
            finally:
                q.close()
            if job and str(job["status"]) in ("pending", "processing", "partial"):
                print(json.dumps({"ok": True, "job_id": old_id, "reused": True}, ensure_ascii=False))
                return 0

    c = Candidate(
        platform=platform,
        video_id=video_id,
        url=f"manual://{workflow}/{video_id}",
        original_url=f"manual://{workflow}/{video_id}",
        title=title,
        topic_id="manual:cli",
        score=9999.0,
    )
    q = QueueDB()
    try:
        result = q.enqueue(
            c,
            priority="normal",
            status="processing",
            workflow=workflow,
            pack_id=f"{platform}_{video_id}",
            meta={
                "workflow": workflow,
                "lane": "media",
                "stages": workflow,
                "manual": True,
                "manual_work_dir": str(work),
                "manual_pid": args.pid,
            },
        )
        if result == "inserted":
            row = q.get_job_by_workflow(platform, video_id, workflow)
            jid = int(row["id"]) if row else 0
        elif result == "skipped_done":
            row = q.get_job_by_workflow(platform, video_id, workflow)
            jid = int(row["id"]) if row else 0
            if jid and str(row["status"]) in ("done", "published"):
                q.requeue_job(jid, allow_done=True)
                q.set_status(jid, "processing")
                # keep it out of the high-priority stale reaper / try scheduler
                q._conn.execute(
                    "UPDATE jobs SET priority='normal' WHERE id=?", (jid,)
                )
                q._conn.commit()
        else:  # ignored — existing pending/processing row
            row = q.get_job_by_workflow(platform, video_id, workflow)
            jid = int(row["id"]) if row else 0
    finally:
        q.close()

    if not jid:
        print(json.dumps({"ok": False, "error": "failed to register job"}, ensure_ascii=False), file=sys.stderr)
        return 1

    try:
        marker.write_text(str(jid), encoding="utf-8")
    except OSError:
        pass
    print(json.dumps(
        {"ok": True, "job_id": jid, "platform": platform, "video_id": video_id,
         "workflow": workflow, "work_dir": str(work)},
        ensure_ascii=False,
    ))
    return 0


def _finish(args: argparse.Namespace, *, ok: bool) -> int:
    q = QueueDB()
    try:
        job = q.get_job(int(args.job_id))
        if job is None:
            print(f"job #{args.job_id} not found", file=sys.stderr)
            return 2
        if ok:
            q.mark_done(int(args.job_id))
        else:
            q.mark_failed(int(args.job_id), args.error or "手动任务标记失败")
    finally:
        q.close()
    print(json.dumps({"ok": True, "job_id": int(args.job_id), "status": "done" if ok else "failed"}, ensure_ascii=False))
    return 0


def cmd_done(args: argparse.Namespace) -> int:
    return _finish(args, ok=True)


def cmd_fail(args: argparse.Namespace) -> int:
    return _finish(args, ok=False)


def cmd_status(_args: argparse.Namespace) -> int:
    q = QueueDB()
    try:
        rows = q._conn.execute(
            "SELECT id,platform,video_id,workflow,status,priority,updated_at FROM jobs "
            "WHERE status IN ('pending','processing','partial') ORDER BY updated_at DESC LIMIT 30"
        ).fetchall()
        out = [
            {"id": r["id"], "platform": r["platform"], "video_id": r["video_id"],
             "workflow": r["workflow"], "status": r["status"], "priority": r["priority"],
             "updated_at": r["updated_at"]}
            for r in rows
        ]
    finally:
        q.close()
    print(json.dumps({"ok": True, "jobs": out, "count": len(out)}, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    st = sub.add_parser("start", help="register a running CLI task")
    st.add_argument("work_dir")
    st.add_argument("--workflow", default="dehardsub")
    st.add_argument("--title", default=None)
    st.add_argument("--platform", default=None)
    st.add_argument("--pid", type=int, default=None)
    st.set_defaults(fn=cmd_start)

    dn = sub.add_parser("done", help="mark a job done")
    dn.add_argument("job_id", type=int)
    dn.set_defaults(fn=cmd_done)

    fl = sub.add_parser("fail", help="mark a job failed")
    fl.add_argument("job_id", type=int)
    fl.add_argument("--error", default=None)
    fl.set_defaults(fn=cmd_fail)

    s = sub.add_parser("status", help="list active jobs")
    s.set_defaults(fn=cmd_status)

    args = p.parse_args()
    return int(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
