"""Vitual MCP tools (Phase A–C + admin + batch upload)."""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from vitual_mcp.client import VitualApiError, VitualClient
from vitual_mcp import resources as res

mcp = FastMCP("vitual_mcp")
_client = VitualClient()


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


class TryClipInput(BaseModel):
    start: float = Field(..., ge=0, description="Clip start time in seconds")
    end: float = Field(..., gt=0, description="Clip end time in seconds")


class TrySubmitUrlsInput(BaseModel):
    urls: list[str] = Field(..., min_length=1, description="Video URLs (YouTube, Bilibili, Douyin, etc.)")
    topic: str = Field(default="general", description="Topic slug for routing/export")
    langs: str | list[str] | None = Field(
        default="site",
        description='Target locales: "site" (16 langs), comma string, or list like ["zh","en"]',
    )
    want_translate: bool = Field(default=True, description="Run subtitle translation")
    want_notes: bool = Field(default=True, description="Run structured notes (WF-02)")
    frames: str = Field(default="auto", description="Frame mode: auto | none | gif | jpg")
    gif_sec: float = Field(default=4.0, ge=1.0, le=20.0, description="GIF length when frames=gif/auto")
    clips: list[TryClipInput] = Field(default_factory=list, description="MP4 clip ranges")
    gif_ranges: list[TryClipInput] = Field(default_factory=list, description="GIF clip ranges")
    sessionid: str | None = Field(default=None, description="Cookie paste for Douyin and similar platforms")
    stages: str | None = Field(default=None, description="For redo on done jobs: all|media|clips|frames")


class TryWaitInput(BaseModel):
    job_id: int = Field(..., ge=1, description="Job ID from vitual_try_submit_urls")
    poll_interval_sec: float = Field(default=5.0, ge=1.0, le=60.0)
    timeout_sec: float = Field(default=3600.0, ge=30.0, le=7200.0)


@mcp.tool(
    name="vitual_health",
    annotations={
        "title": "Vitual API health",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
    },
)
async def vitual_health() -> str:
    """Check Vitual FastAPI at :8901. mongo=false is OK for Try-only; start API with yarn api."""
    try:
        return _json(await _client.health())
    except VitualApiError as e:
        return _json({"ok": False, "error": str(e), "hint": "cd subtitle_pipeline && yarn api"})


@mcp.tool(
    name="vitual_admin_status",
    annotations={
        "title": "Vitual admin status",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
    },
)
async def vitual_admin_status() -> str:
    """Queue counts, scheduler, unacked alerts. Requires Mongo + X-Admin-Token."""
    try:
        return _json(await _client.admin_status())
    except VitualApiError as e:
        return _json({"error": str(e), "hint": "Ensure mongod :27018 and VITUAL_ADMIN_TOKEN"})


@mcp.tool(
    name="vitual_try_probe",
    annotations={
        "title": "Probe try URLs",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
    },
)
async def vitual_try_probe(
    urls: list[str] = Field(..., description="URLs to probe for platform and cookie requirements"),
    sessionid: str | None = Field(default=None, description="Optional cookie paste"),
) -> str:
    """Probe platforms before submit. Use when Douyin or cookie-gated sources may fail."""
    try:
        return _json(await _client.try_probe(urls, sessionid=sessionid))
    except VitualApiError as e:
        return _json({"error": str(e)})


@mcp.tool(
    name="vitual_try_duration",
    annotations={
        "title": "Probe video duration",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
    },
)
async def vitual_try_duration(
    url: str = Field(..., min_length=8, description="Video URL"),
) -> str:
    """Return source duration and whether cookies are required."""
    try:
        return _json(await _client.try_duration(url))
    except VitualApiError as e:
        return _json({"error": str(e)})


@mcp.tool(
    name="vitual_try_submit_urls",
    annotations={
        "title": "Submit try URL batch",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
    },
)
async def vitual_try_submit_urls(params: TrySubmitUrlsInput) -> str:
    """Enqueue URL(s) for ASR/translate/notes. Returns job_id — poll with vitual_try_poll or vitual_try_wait."""
    body: dict[str, Any] = {
        "urls": params.urls,
        "topic": params.topic,
        "want_translate": params.want_translate,
        "want_notes": params.want_notes,
        "frames": params.frames,
        "gif_sec": params.gif_sec,
        "clips": [c.model_dump() for c in params.clips],
        "gif_ranges": [c.model_dump() for c in params.gif_ranges],
    }
    if params.langs is not None:
        body["langs"] = params.langs
    if params.sessionid:
        body["sessionid"] = params.sessionid
    if params.stages:
        body["stages"] = params.stages
    try:
        out = await _client.try_submit_urls(body)
        hint = "Poll vitual_try_poll(job_id) or vitual_try_wait(job_id) until status is done or failed."
        return _json({**out, "_hint": hint})
    except VitualApiError as e:
        return _json({"error": str(e), "body": e.body})


@mcp.tool(
    name="vitual_try_poll",
    annotations={
        "title": "Poll try job",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
    },
)
async def vitual_try_poll(
    job_id: int = Field(..., ge=1, description="Job ID from submit"),
) -> str:
    """Get try job status. When status=done, path is like /topics/{topic}/{slug}."""
    try:
        return _json(await _client.try_poll(job_id))
    except VitualApiError as e:
        return _json({"error": str(e), "job_id": job_id})


@mcp.tool(
    name="vitual_try_wait",
    annotations={
        "title": "Wait for try job",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
    },
)
async def vitual_try_wait(params: TryWaitInput) -> str:
    """Poll until job reaches done or failed (or timeout). Prefer over manual vitual_try_poll loops."""
    try:
        out = await _client.try_wait(
            params.job_id,
            poll_interval_sec=params.poll_interval_sec,
            timeout_sec=params.timeout_sec,
        )
        status = str(out.get("status") or "").lower()
        if status == "done":
            out["_hint"] = "Run yarn export-site then open transform dev or build for static feed."
        elif status == "failed":
            out["_hint"] = "Check error field; retry with vitual_try_probe for cookie platforms."
        return _json(out)
    except VitualApiError as e:
        return _json({"error": str(e), "job_id": params.job_id, "body": e.body})


def _admin_err(e: VitualApiError) -> str:
    return _json({"error": str(e), "body": e.body, "hint": "mongod :27018 + VITUAL_ADMIN_TOKEN"})


@mcp.tool(name="vitual_admin_inbox")
async def vitual_admin_inbox(
    url: str = Field(..., min_length=8, description="Video URL to enqueue"),
    topic: str = Field(default="inbox", description="Topic slug"),
    title: str | None = Field(default=None, description="Optional display title"),
) -> str:
    """Enqueue URL via admin API (background). Syncs to Mongo source_links when Mongo is up."""
    try:
        out = await _client.admin_inbox(url, topic=topic, title=title)
        return _json({**out, "_hint": "Check vitual_admin_status or vitual_admin_batch next."})
    except VitualApiError as e:
        return _admin_err(e)


@mcp.tool(name="vitual_admin_discover")
async def vitual_admin_discover(
    topic: str | None = Field(default=None, description="Topic from discover/topics.yaml"),
    dry_run: bool = Field(default=False, description="Discover without writing queue"),
    mock: bool = Field(default=False, description="Offline mock discover"),
) -> str:
    """Run scheduled discover job via admin API (background)."""
    try:
        return _json(await _client.admin_discover(topic=topic, dry_run=dry_run, mock=mock))
    except VitualApiError as e:
        return _admin_err(e)


@mcp.tool(name="vitual_admin_batch")
async def vitual_admin_batch(
    fast: bool = Field(default=True, description="batch:fast style (site langs, no-multipass)"),
    limit: int = Field(default=1, ge=1, le=20, description="Max pending jobs to process"),
    requeue_failed: bool = Field(default=True, description="Retry failed jobs first"),
) -> str:
    """Process pending queue jobs (ASR/translate/notes). Background; one lock at a time."""
    try:
        out = await _client.admin_batch(fast=fast, limit=limit, requeue_failed=requeue_failed)
        return _json({**out, "_hint": "Poll vitual_admin_status; then vitual_admin_export."})
    except VitualApiError as e:
        return _admin_err(e)


@mcp.tool(name="vitual_admin_export")
async def vitual_admin_export() -> str:
    """Export content.db to transform/content/articles.json (background)."""
    try:
        out = await _client.admin_export()
        return _json({**out, "_hint": "Then cd ../transform && npm run build"})
    except VitualApiError as e:
        return _admin_err(e)


@mcp.tool(name="vitual_admin_links")
async def vitual_admin_links(
    limit: int = Field(default=100, ge=1, le=500, description="Max links to return"),
) -> str:
    """List Mongo source_links (original_url + metadata)."""
    try:
        return _json(await _client.admin_links(limit=limit))
    except VitualApiError as e:
        return _admin_err(e)


@mcp.tool(name="vitual_admin_logs")
async def vitual_admin_logs(
    limit: int = Field(default=200, ge=1, le=1000, description="Max log rows"),
) -> str:
    """Recent ops logs from Mongo."""
    try:
        return _json(await _client.admin_logs(limit=limit))
    except VitualApiError as e:
        return _admin_err(e)


@mcp.tool(name="vitual_admin_alerts")
async def vitual_admin_alerts(
    unacked_only: bool = Field(default=False, description="Only unacknowledged alerts"),
) -> str:
    """List ops alerts (failures, webhook events)."""
    try:
        return _json(await _client.admin_alerts(unacked=unacked_only))
    except VitualApiError as e:
        return _admin_err(e)


@mcp.tool(name="vitual_admin_ack_alert")
async def vitual_admin_ack_alert(
    alert_id: str = Field(..., min_length=1, description="Alert id from vitual_admin_alerts"),
) -> str:
    """Acknowledge an alert."""
    try:
        return _json(await _client.admin_ack_alert(alert_id))
    except VitualApiError as e:
        return _admin_err(e)


@mcp.tool(name="vitual_admin_runs")
async def vitual_admin_runs(
    limit: int = Field(default=50, ge=1, le=200, description="Max run records"),
) -> str:
    """Recent discover/batch/export run history."""
    try:
        return _json(await _client.admin_runs(limit=limit))
    except VitualApiError as e:
        return _admin_err(e)


@mcp.tool(name="vitual_try_submit_upload")
async def vitual_try_submit_upload(
    file_path: str = Field(..., min_length=1, description="Absolute or relative path to local video file"),
    topic: str = Field(default="general", description="Topic slug"),
    langs: str | None = Field(default="site", description='Target langs: "site" or JSON list string'),
    frames: str = Field(default="auto", description="auto | none | gif | jpg"),
    gif_sec: float = Field(default=4.0, ge=1.0, le=20.0),
) -> str:
    """Upload local video for ASR/translate/notes. Returns job_id — use vitual_try_wait."""
    lang_val: str | list[str] | None = langs
    if langs and langs.strip().startswith("["):
        try:
            parsed = json.loads(langs)
            if isinstance(parsed, list):
                lang_val = parsed
        except json.JSONDecodeError:
            pass
    try:
        out = await _client.try_submit_upload(
            file_path,
            topic=topic,
            frames=frames,
            gif_sec=gif_sec,
            langs=lang_val,
        )
        job_id = out.get("job_id")
        hint = f"Poll vitual_try_wait with job_id={job_id}."
        return _json({**out, "_hint": hint})
    except VitualApiError as e:
        return _json({"error": str(e), "body": e.body, "file_path": file_path})


@mcp.tool(name="vitual_try_submit_uploads")
async def vitual_try_submit_uploads(
    file_paths: list[str] = Field(..., min_length=1, description="Local video paths (batch)"),
    topic: str = Field(default="general", description="Topic slug"),
    langs: str | None = Field(default="site", description='Target langs: "site" or JSON list string'),
    frames: str = Field(default="auto", description="auto | none | gif | jpg"),
    gif_sec: float = Field(default=4.0, ge=1.0, le=20.0),
    want_translate: bool = Field(default=True),
    want_notes: bool = Field(default=True),
) -> str:
    """Upload multiple local videos via /api/try/uploads. Poll each job_id with vitual_try_wait."""
    lang_val: str | list[str] | None = langs
    if langs and langs.strip().startswith("["):
        try:
            parsed = json.loads(langs)
            if isinstance(parsed, list):
                lang_val = parsed
        except json.JSONDecodeError:
            pass
    try:
        out = await _client.try_submit_uploads(
            file_paths,
            topic=topic,
            frames=frames,
            gif_sec=gif_sec,
            langs=lang_val,
            want_translate=want_translate,
            want_notes=want_notes,
        )
        job_ids = [j.get("job_id") for j in (out.get("jobs") or []) if j.get("job_id")]
        hint = f"Poll vitual_try_wait for job_ids={job_ids} (starter job_id={out.get('job_id')})."
        return _json({**out, "_hint": hint})
    except VitualApiError as e:
        return _json({"error": str(e), "body": e.body, "file_paths": file_paths})


@mcp.tool(name="vitual_try_wait_all")
async def vitual_try_wait_all(
    job_ids: list[int] = Field(..., min_length=1, description="Job IDs from batch submit"),
    poll_interval_sec: float = Field(default=5.0, ge=1.0, le=60.0),
    timeout_sec: float = Field(default=3600.0, ge=30.0, le=7200.0),
) -> str:
    """Wait for multiple try jobs in parallel. Use after vitual_try_submit_urls or vitual_try_submit_uploads."""
    try:
        out = await _client.try_wait_all(
            job_ids,
            poll_interval_sec=poll_interval_sec,
            timeout_sec=timeout_sec,
        )
        if out.get("all_done"):
            out["_hint"] = "Run yarn export-site for static feed."
        else:
            out["_hint"] = "Inspect failed jobs[].error; retry probe for cookie platforms."
        return _json(out)
    except VitualApiError as e:
        return _json({"error": str(e), "job_ids": job_ids})


# --- Resources (Phase C) ---


@mcp.resource(
    "vitual://export/articles",
    name="export_articles",
    description="Full transform/content/articles.json after yarn export-site",
    mime_type="application/json",
)
def vitual_resource_articles() -> str:
    return res.articles_full()


@mcp.resource(
    "vitual://export/articles/index",
    name="export_articles_index",
    description="Lightweight slug/title index of exported articles (no cues body)",
    mime_type="application/json",
)
def vitual_resource_articles_index() -> str:
    return res.articles_index()


@mcp.resource(
    "vitual://export/articles/{slug}",
    name="export_article",
    description="Single exported article by slug",
    mime_type="application/json",
)
def vitual_resource_article(slug: str) -> str:
    return res.article_by_slug(slug)


@mcp.resource(
    "vitual://api/health",
    name="api_health",
    description="Live GET /health snapshot from FastAPI :8901",
    mime_type="application/json",
)
async def vitual_resource_health() -> str:
    return await res.api_health()
