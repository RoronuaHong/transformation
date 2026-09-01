"""FastAPI ops server: schedule + admin APIs + workbench Try. Public frontend is transform/."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from api.mongo import MongoStore
from api.runner import current_status, run_locked, sync_links_from_queue
from api.security import require_admin
from api.settings import Settings, get_settings

_store: MongoStore | None = None
_scheduler = None


def store() -> MongoStore:
    if _store is None:
        raise HTTPException(status_code=503, detail="mongo not ready")
    return _store


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _store, _scheduler
    import os

    from pipeline import find_ffmpeg

    try:
        os.environ.setdefault("FFMPEG_PATH", find_ffmpeg())
    except FileNotFoundError:
        pass

    settings = get_settings()
    mongo_ready = False
    try:
        candidate = MongoStore(settings)
        candidate.ping()
        _store = candidate
        mongo_ready = True
        _store.write_log("info", "api", "ops api started (mongo)")
    except Exception as e:
        # Workbench /api/try/* does not need Mongo; keep API up for local try.
        from api.memory_store import MemoryStore

        print(
            f"WARNING: MongoDB not reachable at {settings.mongo_uri} ({e}). "
            "Using in-memory store; admin persistence disabled until mongod :27018 is up."
        )
        _store = MemoryStore()
        _store.write_log("info", "api", "ops api started (memory fallback)")
    if settings.schedule_enabled and mongo_ready:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger

        _scheduler = BackgroundScheduler(timezone="Asia/Shanghai")

        def _cron_parts(expr: str) -> dict[str, str]:
            minute, hour, day, month, dow = expr.split()
            return {
                "minute": minute,
                "hour": hour,
                "day": day,
                "month": month,
                "day_of_week": dow,
            }

        _scheduler.add_job(
            lambda: _job_discover(settings),
            CronTrigger(**_cron_parts(settings.schedule_discover)),
            id="daily_discover",
            replace_existing=True,
        )
        _scheduler.add_job(
            lambda: _job_batch(settings, fast=False),
            CronTrigger(**_cron_parts(settings.schedule_batch)),
            id="daily_batch",
            replace_existing=True,
        )
        _scheduler.start()
        _store.write_log(
            "info",
            "scheduler",
            f"discover={settings.schedule_discover} batch={settings.schedule_batch}",
        )
    yield
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
    if _store is not None:
        _store.write_log("info", "api", "ops api stopping")
        _store.close()
        _store = None


app = FastAPI(
    title="Vitual ops (backend)",
    description="Admin/ops API + workbench Try. Public frontend is Next.js `transform/` (port 3000). "
    "This service is the management backend (port 8901) + scheduler.",
    lifespan=lifespan,
    docs_url="/admin/docs",
    redoc_url=None,
)

_settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _settings.cors_origins.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class InboxBody(BaseModel):
    url: str = Field(..., min_length=8, description="original pasted URL")
    topic: str = "inbox"
    title: str | None = None


class DiscoverBody(BaseModel):
    topic: str | None = None
    dry_run: bool = False
    mock: bool = False


class BatchBody(BaseModel):
    fast: bool = True
    limit: int = 1
    requeue_failed: bool = True


def _job_discover(settings: Settings, body: DiscoverBody | None = None) -> dict:
    from discover.run_discover import main as discover_main

    b = body or DiscoverBody()
    argv: list[str] = []
    if b.topic:
        argv += ["--topic", b.topic]
    if b.dry_run:
        argv.append("--dry-run")
    if b.mock:
        argv.append("--mock")
    return run_locked(
        store(),
        settings,
        "discover",
        lambda: discover_main(argv),
    )


def _job_batch(settings: Settings, *, fast: bool, limit: int = 1, requeue: bool = True) -> dict:
    from discover.run_batch import main as batch_main

    argv = ["--langs", "site", "--limit", str(limit)]
    if fast:
        argv += ["--no-multipass", "--device", "cpu"]
    if requeue:
        argv.append("--requeue-failed")
    return run_locked(store(), settings, "batch", lambda: batch_main(argv))


def _job_export(settings: Settings) -> dict:
    from discover.export_site import main as export_main

    return run_locked(store(), settings, "export", lambda: export_main([]))


def _job_inbox(settings: Settings, body: InboxBody) -> dict:
    from discover.run_inbox import main as inbox_main

    argv = ["--url", body.url, "--topic", body.topic]
    if body.title:
        argv += ["--title", body.title]
    result = run_locked(store(), settings, "inbox", lambda: inbox_main(argv))
    sync_links_from_queue(store(), source="inbox")
    return result


@app.get("/health")
def health() -> dict[str, Any]:
    mongo_ok = False
    store_kind = "none"
    try:
        s = store()
        s.ping()
        mongo_ok = getattr(s, "client", None) is not None
        store_kind = "mongo" if mongo_ok else "memory"
    except Exception:
        mongo_ok = False
    return {
        "ok": True,
        "role": "backend-ops",
        "frontend": "transform/ Next.js :3000",
        "admin": "admin/ Next.js :3001",
        "mongo": mongo_ok,
        "store": store_kind,
        "scheduler": current_status(),
    }


@app.get("/admin/status", dependencies=[Depends(require_admin)])
def admin_status() -> dict[str, Any]:
    from discover.queue_db import QueueDB

    q = QueueDB()
    try:
        queue = q.count_by_status()
    finally:
        q.close()
    jobs = []
    if _scheduler is not None:
        for j in _scheduler.get_jobs():
            jobs.append({"id": j.id, "next_run": str(j.next_run_time)})
    return {
        "queue": queue,
        "run": current_status(),
        "schedule": jobs,
        "unacked_alerts": len(store().list_alerts(unacked_only=True, limit=50)),
    }


@app.post("/admin/inbox", dependencies=[Depends(require_admin)])
def admin_inbox(
    body: InboxBody, background: BackgroundTasks, settings: Settings = Depends(get_settings)
) -> dict:
    background.add_task(_job_inbox, settings, body)
    return {"accepted": True, "action": "inbox", "original_url": body.url}


@app.post("/admin/discover", dependencies=[Depends(require_admin)])
def admin_discover(
    body: DiscoverBody, background: BackgroundTasks, settings: Settings = Depends(get_settings)
) -> dict:
    background.add_task(_job_discover, settings, body)
    return {"accepted": True, "action": "discover"}


@app.post("/admin/batch", dependencies=[Depends(require_admin)])
def admin_batch(
    body: BatchBody, background: BackgroundTasks, settings: Settings = Depends(get_settings)
) -> dict:
    background.add_task(
        _job_batch, settings, fast=body.fast, limit=body.limit, requeue=body.requeue_failed
    )
    return {"accepted": True, "action": "batch"}


@app.post("/admin/export", dependencies=[Depends(require_admin)])
def admin_export(
    background: BackgroundTasks, settings: Settings = Depends(get_settings)
) -> dict:
    background.add_task(_job_export, settings)
    return {"accepted": True, "action": "export"}


@app.get("/admin/links", dependencies=[Depends(require_admin)])
def admin_links(limit: int = 100) -> dict:
    sync_links_from_queue(store(), source="sync")
    return {"items": store().list_links(limit=limit)}


@app.get("/admin/logs", dependencies=[Depends(require_admin)])
def admin_logs(limit: int = 200) -> dict:
    return {"items": store().list_logs(limit=limit)}


@app.get("/admin/alerts", dependencies=[Depends(require_admin)])
def admin_alerts(unacked: bool = False) -> dict:
    return {"items": store().list_alerts(unacked_only=unacked)}


@app.post("/admin/alerts/{alert_id}/ack", dependencies=[Depends(require_admin)])
def admin_ack(alert_id: str) -> dict:
    ok = store().ack_alert(alert_id)
    if not ok:
        raise HTTPException(status_code=404, detail="alert not found")
    return {"ok": True}


@app.get("/admin/runs", dependencies=[Depends(require_admin)])
def admin_runs(limit: int = 50) -> dict:
    return {"items": store().list_runs(limit=limit)}


# --- Public try: single URL or upload (C-end preview) ---


class TryClip(BaseModel):
    start: float = Field(..., ge=0)
    end: float = Field(..., gt=0)


class TryUrlBody(BaseModel):
    url: str = Field(..., min_length=8)
    topic: str = "general"
    frames: str = Field(
        default="auto",
        description="auto | none | gif | jpg — user frame preference",
    )
    gif_sec: float = Field(default=4.0, ge=1.0, le=20.0)
    gif_ranges: list[TryClip] = Field(default_factory=list)
    clips: list[TryClip] = Field(default_factory=list)
    sessionid: str | None = Field(
        default=None,
        description="Optional cookies: session token or Netscape dump for the source site",
    )
    stages: str | None = Field(
        default=None,
        description="On already-done jobs: all|media|clips|frames (default auto)",
    )
    langs: list[str] | str | None = Field(
        default=None,
        description="Target locales (site pack). Omit or empty = all site langs.",
    )
    want_translate: bool = Field(default=True, description="Run subtitle translation")
    want_notes: bool = Field(default=True, description="Run structured notes")
    want_enhance: bool = Field(default=False, description="WF-04 sharpen / mild upscale")
    want_compress: bool = Field(default=False, description="WF-05 shrink file size")
    want_concat: bool = Field(default=False, description="WF-06 concat clip ranges")
    media_opts: dict | None = Field(default=None, description="enhance_strength / compress_height / compress_crf")


class TryProbeBody(BaseModel):
    urls: list[str] = Field(default_factory=list)
    sessionid: str | None = Field(default=None, description="Cookie paste not yet saved locally")


class TryUrlEntry(BaseModel):
    url: str = Field(..., min_length=8)
    override: bool = False
    frames: str | None = None
    gif_sec: float | None = Field(default=None, ge=1.0, le=20.0)
    gif_ranges: list[TryClip] = Field(default_factory=list)
    clips: list[TryClip] = Field(default_factory=list)


class TryUrlsBody(BaseModel):
    urls: list[str] = Field(..., min_length=1)
    entries: list[TryUrlEntry] = Field(default_factory=list)
    topic: str = "general"
    frames: str = Field(default="auto")
    gif_sec: float = Field(default=4.0, ge=1.0, le=20.0)
    gif_ranges: list[TryClip] = Field(default_factory=list)
    clips: list[TryClip] = Field(default_factory=list)
    sessionid: str | None = None
    stages: str | None = None
    langs: list[str] | str | None = None
    want_translate: bool = True
    want_notes: bool = True
    want_enhance: bool = False
    want_compress: bool = False
    want_concat: bool = False
    media_opts: dict | None = None


@app.post("/api/try/probe")
def try_probe(body: TryProbeBody) -> dict:
    from api.try_service import probe_urls

    urls = [u.strip() for u in body.urls if (u or "").strip()]
    return probe_urls(urls, pasted_cookies=body.sessionid)


@app.post("/api/try/urls")
def try_urls(body: TryUrlsBody, background: BackgroundTasks) -> dict:
    from api.try_service import job_snapshot, run_try_job, submit_urls, try_status

    clip_dicts = [{"start": c.start, "end": c.end} for c in body.clips]
    gif_dicts = [{"start": c.start, "end": c.end} for c in body.gif_ranges]
    urls = [u.strip() for u in body.urls if (u or "").strip()]
    entry_dicts = [
        {
            "url": e.url.strip(),
            "override": e.override,
            **({"frames": e.frames} if e.frames is not None else {}),
            **({"gif_sec": e.gif_sec} if e.gif_sec is not None else {}),
            "gif_ranges": [{"start": c.start, "end": c.end} for c in e.gif_ranges],
            "clips": [{"start": c.start, "end": c.end} for c in e.clips],
        }
        for e in body.entries
        if (e.url or "").strip()
    ]
    out = submit_urls(
        urls,
        entries=entry_dicts,
        topic=body.topic,
        frames=body.frames,
        gif_sec=body.gif_sec,
        clips=clip_dicts,
        gif_ranges=gif_dicts,
        sessionid=body.sessionid,
        langs=body.langs,
        want_translate=body.want_translate,
        want_notes=body.want_notes,
        stages=body.stages,
        want_enhance=body.want_enhance,
        want_compress=body.want_compress,
        want_concat=body.want_concat,
        media_opts=body.media_opts,
    )
    if not out.get("ok"):
        raise HTTPException(status_code=400, detail=out.get("error") or "submit failed")

    starter = out.get("started_job_id")
    if starter and not try_status()["busy"]:
        background.add_task(run_try_job, int(starter))

    enriched = []
    for row in out.get("jobs") or []:
        item = dict(row)
        jid = item.get("job_id")
        if jid:
            snap = job_snapshot(int(jid))
            if snap.get("ok"):
                for key in ("status", "path", "title", "error", "progress", "busy"):
                    if key in snap:
                        item[key] = snap[key]
        enriched.append(item)

    snap = job_snapshot(int(starter)) if starter else {"ok": False}
    return {
        "accepted": True,
        "batch": True,
        "queued": out.get("queued", 0),
        "failed_enqueue": out.get("failed", 0),
        "jobs": enriched,
        "job_id": starter,
        **(snap if snap.get("ok") else {}),
        "status": snap.get("status") or ("pending" if starter else "done"),
    }


@app.post("/api/try/url")
def try_url(body: TryUrlBody, background: BackgroundTasks) -> dict:
    from api.try_service import (
        job_snapshot,
        refresh_try_frames,
        resolve_try_intent,
        run_try_job,
        submit_url,
        try_status,
    )

    clip_dicts = [{"start": c.start, "end": c.end} for c in body.clips]
    gif_dicts = [{"start": c.start, "end": c.end} for c in body.gif_ranges]
    out = submit_url(
        body.url,
        topic=body.topic,
        frames=body.frames,
        gif_sec=body.gif_sec,
        clips=clip_dicts,
        gif_ranges=gif_dicts,
        sessionid=body.sessionid,
        langs=body.langs,
    )
    if not out.get("ok"):
        raise HTTPException(status_code=400, detail=out.get("error") or "submit failed")
    job_id = int(out["job_id"])
    enqueue = out.get("enqueue")

    if enqueue in ("inserted", "requeued", "already_pending"):
        intent = {
            "intent": "full",
            "stages": "all",
            "langs": out.get("langs"),
            "reason": "new_or_retry",
        }
    elif enqueue == "already_done":
        intent = resolve_try_intent(
            job_status="done",
            stages=body.stages,
            frames=body.frames,
            has_clips=bool(clip_dicts),
            has_gif_ranges=bool(gif_dicts),
            new_langs=out.get("langs"),
            prev_langs=out.get("prev_langs"),
            new_frame_opts=out.get("frame_opts"),
            prev_frame_opts=out.get("prev_frame_opts"),
        )
    else:
        intent = resolve_try_intent(
            job_status=out.get("job_status"),
            stages=body.stages,
            frames=body.frames,
            has_clips=bool(clip_dicts),
            has_gif_ranges=bool(gif_dicts),
            new_langs=out.get("langs"),
            prev_langs=out.get("prev_langs"),
            new_frame_opts=out.get("frame_opts"),
            prev_frame_opts=out.get("prev_frame_opts"),
        )

    if enqueue == "already_done":
        from discover.queue_db import QueueDB
        from functools import partial

        if intent["intent"] == "noop":
            snap = job_snapshot(job_id)
            return {
                "accepted": True,
                "noop": True,
                "intent": intent,
                "frame_opts": out.get("frame_opts"),
                "langs": out.get("langs"),
                **snap,
                "status": snap.get("status") or "done",
            }

        if try_status()["busy"]:
            raise HTTPException(status_code=409, detail="another job is processing")

        if intent["intent"] in ("clips", "frames", "media"):
            background.add_task(
                partial(refresh_try_frames, job_id, stages=intent["stages"])
            )
            snap = job_snapshot(job_id)
            return {
                "accepted": True,
                "frames_only": True,
                "clips_only": intent["intent"] == "clips",
                "intent": intent,
                "stages": intent["stages"],
                "frame_opts": out.get("frame_opts"),
                "langs": out.get("langs"),
                **snap,
                "status": "processing",
            }

        # langs changed → post (reuse ASR)
        queue = QueueDB()
        try:
            queue.set_meta(
                job_id, {"stages": intent["stages"], "langs": intent["langs"]}
            )
            if not queue.requeue_job(job_id, allow_done=True):
                raise HTTPException(
                    status_code=409, detail="could not requeue finished job"
                )
        finally:
            queue.close()
        background.add_task(run_try_job, job_id)
        snap = job_snapshot(job_id)
        return {
            "accepted": True,
            "frames_only": False,
            "requeued": True,
            "intent": intent,
            "stages": intent["stages"],
            "frame_opts": out.get("frame_opts"),
            "langs": out.get("langs"),
            **snap,
            "status": "pending",
        }

    if try_status()["busy"]:
        raise HTTPException(status_code=409, detail="another job is processing")
    if enqueue == "already_processing":
        raise HTTPException(status_code=409, detail="another job is processing")
    background.add_task(run_try_job, job_id)
    snap = job_snapshot(job_id)
    return {
        "accepted": True,
        "intent": intent,
        "frame_opts": out.get("frame_opts"),
        "langs": out.get("langs"),
        **snap,
    }


@app.post("/api/try/uploads")
async def try_uploads(
    background: BackgroundTasks,
    files: list[UploadFile] = File(...),
    entries: str = Form("[]"),
    topic: str = Form("general"),
    frames: str = Form("auto"),
    gif_sec: float = Form(4.0),
    gif_ranges: str = Form("[]"),
    clips: str = Form("[]"),
    langs: str = Form(""),
    want_translate: str = Form("true"),
    want_notes: str = Form("true"),
    want_enhance: str = Form("false"),
    want_compress: str = Form("false"),
    want_concat: str = Form("false"),
    media_opts: str = Form("{}"),
    stages: str = Form(""),
) -> dict:
    from api.try_service import job_snapshot, run_try_job, submit_uploads, try_status

    if not files:
        raise HTTPException(status_code=400, detail="no files")
    try:
        clip_raw = json.loads(clips or "[]")
    except json.JSONDecodeError:
        clip_raw = []
    try:
        gif_raw = json.loads(gif_ranges or "[]")
    except json.JSONDecodeError:
        gif_raw = []
    try:
        entry_raw = json.loads(entries or "[]")
    except json.JSONDecodeError:
        entry_raw = []
    entry_by_name: dict[str, dict] = {}
    if isinstance(entry_raw, list):
        for row in entry_raw:
            if isinstance(row, dict):
                name = str(row.get("filename") or "").strip()
                if name:
                    entry_by_name[name] = row

    lang_list: list[str] | str | None = None
    raw_langs = (langs or "").strip()
    if raw_langs:
        try:
            parsed = json.loads(raw_langs)
            lang_list = parsed if isinstance(parsed, list) else raw_langs
        except json.JSONDecodeError:
            lang_list = raw_langs

    clip_dicts = clip_raw if isinstance(clip_raw, list) else []
    gif_dicts = gif_raw if isinstance(gif_raw, list) else []
    items: list[dict] = []
    for f in files:
        name = f.filename or "upload.mp4"
        data = await f.read()
        entry = entry_by_name.get(name)
        dur = None
        if isinstance(entry, dict) and entry.get("duration_sec") is not None:
            try:
                dur = float(entry["duration_sec"])
            except (TypeError, ValueError):
                dur = None
        items.append(
            {
                "filename": name,
                "bytes": data,
                "duration_sec": dur,
                "entry": entry,
            }
        )

    wt = str(want_translate or "true").strip().lower() not in ("0", "false", "no", "off")
    wn = str(want_notes or "true").strip().lower() not in ("0", "false", "no", "off")
    we = str(want_enhance or "false").strip().lower() not in ("0", "false", "no", "off")
    wc = str(want_compress or "false").strip().lower() not in ("0", "false", "no", "off")
    wcat = str(want_concat or "false").strip().lower() not in ("0", "false", "no", "off")
    try:
        media_opts_dict = json.loads(media_opts or "{}")
        if not isinstance(media_opts_dict, dict):
            media_opts_dict = {}
    except json.JSONDecodeError:
        media_opts_dict = {}
    stage_s = (stages or "").strip() or None

    out = submit_uploads(
        items,
        topic=topic,
        frames=frames,
        gif_sec=gif_sec,
        clips=clip_dicts,
        gif_ranges=gif_dicts,
        langs=lang_list,
        want_translate=wt,
        want_notes=wn,
        stages=stage_s,
        want_enhance=we,
        want_compress=wc,
        want_concat=wcat,
        media_opts=media_opts_dict,
    )
    if not out.get("ok"):
        raise HTTPException(status_code=400, detail=out.get("error") or "submit failed")

    starter = out.get("started_job_id")
    if starter and not try_status()["busy"]:
        background.add_task(run_try_job, int(starter))

    enriched = []
    for row in out.get("jobs") or []:
        item = dict(row)
        jid = item.get("job_id")
        if jid:
            snap = job_snapshot(int(jid))
            if snap.get("ok"):
                for key in ("status", "path", "title", "error", "progress", "busy"):
                    if key in snap:
                        item[key] = snap[key]
        enriched.append(item)

    snap = job_snapshot(int(starter)) if starter else {"ok": False}
    return {
        "accepted": True,
        "batch": True,
        "queued": out.get("queued", 0),
        "failed_enqueue": out.get("failed", 0),
        "jobs": enriched,
        "job_id": starter,
        **(snap if snap.get("ok") else {}),
        "status": snap.get("status") or ("pending" if starter else "done"),
    }


@app.post("/api/try/upload")
async def try_upload(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    topic: str = Form("general"),
    frames: str = Form("auto"),
    gif_sec: float = Form(4.0),
    gif_ranges: str = Form("[]"),
    clips: str = Form("[]"),
    langs: str = Form(""),
) -> dict:
    from api.try_service import job_snapshot, run_try_job, submit_upload, try_status

    if try_status()["busy"]:
        raise HTTPException(status_code=409, detail="another job is processing")
    try:
        clip_raw = json.loads(clips or "[]")
    except json.JSONDecodeError:
        clip_raw = []
    try:
        gif_raw = json.loads(gif_ranges or "[]")
    except json.JSONDecodeError:
        gif_raw = []
    lang_list: list[str] | str | None = None
    raw_langs = (langs or "").strip()
    if raw_langs:
        try:
            parsed = json.loads(raw_langs)
            lang_list = parsed if isinstance(parsed, list) else raw_langs
        except json.JSONDecodeError:
            lang_list = raw_langs
    data = await file.read()
    out = submit_upload(
        data,
        file.filename or "upload.mp4",
        topic=topic,
        frames=frames,
        gif_sec=gif_sec,
        clips=clip_raw if isinstance(clip_raw, list) else [],
        gif_ranges=gif_raw if isinstance(gif_raw, list) else [],
        langs=lang_list,
    )
    if not out.get("ok"):
        raise HTTPException(status_code=400, detail=out.get("error") or "upload failed")
    job_id = int(out["job_id"])
    background.add_task(run_try_job, job_id)
    snap = job_snapshot(job_id)
    return {"accepted": True, "frame_opts": out.get("frame_opts"), **snap}


@app.get("/api/try/duration")
def try_duration(url: str) -> dict:
    """Probe source length + cookie requirement for try form (always JSON)."""
    from api.try_service import probe_url_duration

    return probe_url_duration(url)

@app.get("/api/try/active")
def try_active(limit: int = 10) -> dict:
    from api.try_service import list_active_try_jobs

    return list_active_try_jobs(limit=limit)


@app.post("/api/try/queue/pause")
def try_queue_pause() -> dict:
    from api.try_service import pause_try_queue

    return pause_try_queue()


@app.post("/api/try/queue/resume")
def try_queue_resume() -> dict:
    from api.try_service import resume_try_queue

    return resume_try_queue()


@app.post("/api/try/{job_id}/cancel")
def try_cancel(job_id: int) -> dict:
    from api.try_service import cancel_try_job

    out = cancel_try_job(job_id)
    if not out.get("ok"):
        raise HTTPException(status_code=409, detail=out.get("error") or "cancel failed")
    return out


@app.post("/api/try/{job_id}/retry")
def try_retry(job_id: int, background: BackgroundTasks) -> dict:
    from api.try_service import job_snapshot, retry_try_job, run_try_job, try_status

    out = retry_try_job(job_id)
    if not out.get("ok"):
        raise HTTPException(status_code=409, detail=out.get("error") or "retry failed")
    if out.get("status") == "pending" and not try_status()["busy"]:
        background.add_task(run_try_job, job_id)
        snap = job_snapshot(job_id)
        if snap.get("ok"):
            out.update(snap)
    return out


@app.get("/api/try/{job_id}")
def try_poll(job_id: int) -> dict:
    from api.try_service import job_snapshot

    snap = job_snapshot(job_id)
    if not snap.get("ok"):
        raise HTTPException(status_code=404, detail=snap.get("error") or "not found")
    return snap
