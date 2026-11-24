"""Public single-video try: paste URL or upload a file → notes + GIF."""

from __future__ import annotations

import json
import shutil
import threading
import time
import uuid
from pathlib import Path

from discover.content_db import ContentDB
from discover.export_site import main as export_main
from discover.models import Candidate
from discover.queue_db import QueueDB
from discover.run_inbox import canonical_url, parse_url
from fetch_media import ensure_source_video, find_ffmpeg, _looks_like_netscape
from job_layout import existing_wav, job_media_dir, list_locale_srts
from langs import DEFAULT_PACK, PACKS, coalesce_source_lang, normalize_lang, resolve_targets
from workflows import (
    ARTIFACT_SOURCE_SRT,
    LANE_CONTENT,
    LANE_MEDIA,
    expand_workflow_tasks,
    infer_workflow_progress,
    lane_for_workflow,
    list_pack_artifacts,
    pack_id_for,
    workflow_products,
    write_pack_manifest,
)

# Try 允许的语言 = 字幕产物默认包(20 语);站点 UI 的 16 语 locales 与此解耦。
LANG_CODES = tuple(PACKS[DEFAULT_PACK])

# Platforms that require login cookies for anonymous download.
COOKIE_POLICY: dict[str, dict] = {
    "douyin": {"required": True, "label": "Douyin"},
    "bilibili": {"required": False, "label": "Bilibili"},
    "youtube": {"required": False, "soft": True, "label": "YouTube"},
    "hls": {"required": False, "soft": True, "label": "HLS/m3u8"},
}


def _cookies_file_covers_platform(platform: str) -> bool:
    from fetch_media import cookie_domain_for, resolve_cookies_file

    cf = resolve_cookies_file()
    if cf is None:
        return False
    try:
        text = cf.read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        return False
    host = cookie_domain_for(platform).lstrip(".").lower()
    return host in text


def cookies_satisfied(platform: str, *, pasted: str | None = None) -> bool:
    pol = COOKIE_POLICY.get(platform) or {}
    if not pol.get("required"):
        return True
    raw = (pasted or "").strip()
    if raw:
        if _looks_like_netscape(raw):
            from fetch_media import cookie_domain_for

            host = cookie_domain_for(platform).lstrip(".").lower()
            return host in raw.lower()
        return True
    return _cookies_file_covers_platform(platform)


def apply_batch_cookies(pasted: str | None, urls: list[str]) -> None:
    """Merge user cookie paste once before a batch enqueue."""
    from fetch_media import upsert_try_cookies

    raw = (pasted or "").strip()
    if not raw:
        return
    if _looks_like_netscape(raw):
        upsert_try_cookies(raw, platform="douyin", url=urls[0] if urls else "")
        return
    platforms: set[str] = set()
    for url in urls:
        try:
            platform, _ = parse_url(url)
        except ValueError:
            continue
        if (COOKIE_POLICY.get(platform) or {}).get("required"):
            platforms.add(platform)
    if not platforms:
        try:
            platform, _ = parse_url(urls[0])
            platforms.add(platform)
        except ValueError:
            platforms.add("douyin")
    for platform in sorted(platforms):
        upsert_try_cookies(raw, platform=platform, url=urls[0] if urls else "")


def schedule_next_try_job(*, skip_job_id: int | None = None) -> None:
    """Fill free lane slots with the next runnable high-priority pending jobs."""

    def _runner() -> None:
        time.sleep(0.25)
        if _queue_paused:
            return
        q = QueueDB()
        try:
            rows = q._conn.execute(
                """
                SELECT id FROM jobs
                WHERE status='pending' AND priority='high'
                ORDER BY created_at ASC
                """
            ).fetchall()
            started = 0
            for row in rows:
                jid = int(row["id"])
                if skip_job_id is not None and jid == skip_job_id:
                    continue
                job = q.get_job(jid)
                if job is None:
                    continue
                if not _deps_satisfied(q, job):
                    continue
                lane = _lane_for_job(job)
                if not _try_acquire_slot(lane, jid):
                    continue
                # Slot reserved; worker thread runs the job (releases in finally).
                threading.Thread(
                    target=run_try_job,
                    kwargs={"job_id": jid, "_slot_held": True, "_slot_lane": lane},
                    daemon=True,
                ).start()
                started += 1
                # Cap how many we launch per wake to avoid stampede.
                if started >= _max_active_total():
                    break
        finally:
            q.close()

    threading.Thread(target=_runner, daemon=True).start()


def kick_try_scheduler() -> dict:
    """Public entry after enqueue/retry: fill every free dual-lane slot.

    Submits used to call ``run_try_job(starter)`` only — that left the media
    lane idle until the content job finished. Always kick the scheduler instead.
    """
    schedule_next_try_job()
    return try_status()


def notify_artifact_ready(work_dir: Path | str, *, artifact: str = ARTIFACT_SOURCE_SRT) -> None:
    """Called when a pack artifact appears mid-job (e.g. source.srt after ASR)."""
    _ = (work_dir, artifact)
    schedule_next_try_job()


def _job_meta_dict(job) -> dict:
    raw = None
    try:
        raw = job["meta_json"]
    except (KeyError, IndexError, TypeError):
        raw = None
    if not raw:
        return {}
    try:
        meta = json.loads(raw)
        return meta if isinstance(meta, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _pack_has_source_srt(platform: str, video_id: str) -> bool:
    """True when a *detected* source-language SRT exists.

    Without ``sync_meta`` / notes source_lang, only accept literal
    ``source``/``src``/``auto`` stems — never treat leftover ``en.srt`` /
    ``zh.srt`` alone as proof that ASR already ran (that skipped fetch/asr).
    """
    work = _work_dir(platform, video_id)
    if not work.is_dir():
        return False
    stems: set[str] = {"source", "src", "auto"}
    detected = ""
    try:
        sync = work / "media" / "sync_meta.json"
        if sync.is_file():
            meta = json.loads(sync.read_text(encoding="utf-8-sig"))
            for key in ("source_lang", "detected_lang", "lang"):
                raw = str(meta.get(key) or "").strip()
                if raw:
                    detected = raw
                    break
    except Exception:
        pass
    if not detected:
        try:
            from pipeline import load_notes_summary

            for probe in ("zh", "ja", "en", "ko"):
                summary = load_notes_summary(work, "full_16k", probe)
                if summary and summary.get("source_lang"):
                    detected = str(summary["source_lang"]).strip()
                    break
        except Exception:
            pass
    if detected:
        stems.add(detected)
        try:
            from langs import file_tag

            stems.add(file_tag(detected))
        except Exception:
            pass
    candidates: list[Path] = []
    for stem in stems:
        if not stem:
            continue
        candidates.append(work / "subs" / f"{stem}.srt")
        candidates.append(work / f"full_16k_{stem}.srt")
        candidates.append(work / f"full_16k_{stem}_fixed.srt")
    for p in candidates:
        try:
            if p.is_file() and p.stat().st_size > 32:
                return True
        except OSError:
            continue
    return False


def _apply_cancel(queue: QueueDB, job_id: int, *, reason: str = "用户已取消") -> None:
    """Discard sticky cancel flag and seal the job/steps (idempotent)."""
    _cancel_requested.discard(int(job_id))
    job = queue.get_job(int(job_id))
    st = str(job["status"] or "") if job else ""
    if st in ("pending", "processing", "partial"):
        queue.cancel_job(int(job_id), reason=reason)
    else:
        queue.seal_cancelled_steps(int(job_id), reason=reason)


def _deps_satisfied(queue: QueueDB, job) -> bool:
    """REQ-A03: workflow deps + optional artifact gates (e.g. source_srt)."""
    meta = _job_meta_dict(job)
    platform = job["platform"]
    video_id = job["video_id"]
    deps = meta.get("depends_on") or []
    if isinstance(deps, list):
        for dep in deps:
            name = str(dep or "").strip()
            if not name:
                continue
            row = queue.get_job_by_workflow(platform, video_id, name)
            # partial = usable artifacts exist; dependents may proceed.
            if row is None or row["status"] not in ("done", "published", "partial"):
                return False
    arts = meta.get("depends_on_artifacts") or []
    if isinstance(arts, list):
        for art in arts:
            key = str(art or "").strip()
            if not key:
                continue
            if key == ARTIFACT_SOURCE_SRT:
                if not _pack_has_source_srt(platform, video_id):
                    return False
            else:
                return False
    return True


def _cascade_fail_dependents(
    queue: QueueDB,
    platform: str,
    video_id: str,
    failed_workflow: str,
    reason: str,
) -> int:
    """A failed WF must not leave its dependents pending forever.

    Without this, notes/frames wait on a `done` dependency that can never
    arrive — the batch never reaches a terminal state and the form stays
    locked on "处理中…". Transitively mark pending pack jobs whose
    depends_on includes an already failed workflow as failed.
    """
    failed_wfs = {str(failed_workflow or "").strip()}
    changed = 0
    while True:
        rows = queue._conn.execute(
            """
            SELECT id, meta_json FROM jobs
            WHERE platform=? AND video_id=? AND status='pending'
            """,
            (platform, video_id),
        ).fetchall()
        newly: list[int] = []
        for row in rows:
            meta = _job_meta_dict(row)
            deps = meta.get("depends_on") or []
            hit = isinstance(deps, list) and any(
                str(d or "").strip() in failed_wfs for d in deps
            )
            # Notes waiting on source_srt must fail if translate died before SRT.
            if not hit and "translate" in failed_wfs:
                arts = meta.get("depends_on_artifacts") or []
                if (
                    isinstance(arts, list)
                    and ARTIFACT_SOURCE_SRT in [str(a or "").strip() for a in arts]
                    and not _pack_has_source_srt(platform, video_id)
                ):
                    hit = True
            if hit:
                newly.append(int(row["id"]))
        if not newly:
            break
        for jid in newly:
            queue.mark_failed(jid, reason)
            job = queue.get_job(jid)
            wf = _job_workflow(job) if job else ""
            if wf:
                failed_wfs.add(wf)
            changed += 1
        if changed > 50:  # safety valve against cycles
            break
    return changed


def _normalize_input_from_map(raw: dict | None) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in raw.items():
        key = str(k or "").strip()
        val = str(v or "").strip()
        if key and val:
            out[key] = val
    return out


def normalize_try_langs(raw: str | list | None) -> str:
    """Comma list of subtitle-pack locales, or ``DEFAULT_PACK`` when empty / full.

    ``site`` remains an explicit alias for the legacy 16-lang UI pack.
    """
    allowed = {normalize_lang(x) for x in LANG_CODES}
    if isinstance(raw, list):
        items = raw
    else:
        text = (raw or "").strip()
        if not text or text.lower() in ("all", "*", DEFAULT_PACK.lower()):
            return DEFAULT_PACK
        if text.lower() == "site":
            return "site"
        items = text.replace(";", ",").split(",")
    codes: list[str] = []
    for item in items:
        s = str(item or "").strip()
        if not s:
            continue
        tag = normalize_lang(s)
        if tag in allowed and tag not in codes:
            codes.append(tag)
    if not codes or set(codes) >= allowed:
        return DEFAULT_PACK
    return ",".join(codes)


def expand_try_langs(pack: str | None) -> frozenset[str]:
    """Normalized set of subtitle-pack codes for comparison."""
    p = normalize_try_langs(pack)
    if p in PACKS:
        return frozenset(normalize_lang(x) for x in PACKS[p])
    return frozenset(normalize_lang(x) for x in p.split(",") if x.strip())


def langs_equal(a: str | list | None, b: str | list | None) -> bool:
    return expand_try_langs(normalize_try_langs(a)) == expand_try_langs(
        normalize_try_langs(b)
    )


def _job_field(job, key: str, default=None):
    """Read a column from queue job (sqlite3.Row or dict)."""
    if job is None:
        return default
    if hasattr(job, "get"):
        return job.get(key, default)
    try:
        val = job[key]
        return default if val is None else val
    except (KeyError, TypeError, IndexError):
        return default


def job_langs(job) -> str:
    raw = None
    meta_json = _job_field(job, "meta_json")
    if meta_json:
        try:
            meta = json.loads(meta_json)
            if isinstance(meta, dict):
                raw = meta.get("langs")
        except (TypeError, ValueError, json.JSONDecodeError):
            raw = None
    return normalize_try_langs(raw)


def frame_opts_equal(a: dict | None, b: dict | None) -> bool:
    """Compare normalized frame_opts for already-done noop detection."""
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    try:
        sec_a = float(a.get("gif_sec") if a.get("gif_sec") is not None else 4.0)
        sec_b = float(b.get("gif_sec") if b.get("gif_sec") is not None else 4.0)
    except (TypeError, ValueError):
        return False
    return (
        str(a.get("frames") or "auto") == str(b.get("frames") or "auto")
        and abs(sec_a - sec_b) < 0.05
        and list(a.get("clips") or []) == list(b.get("clips") or [])
        and list(a.get("gif_ranges") or []) == list(b.get("gif_ranges") or [])
    )


def entry_frame_opts(
    *,
    global_frames: str = "auto",
    global_gif_sec: float = 4.0,
    global_clips: list | None = None,
    global_gif_ranges: list | None = None,
    entry: dict | None = None,
    duration_sec: float | None = None,
) -> dict:
    """Merge per-link overrides with batch defaults."""
    if not entry or not entry.get("override"):
        return normalize_frame_opts(
            global_frames,
            global_gif_sec,
            global_clips,
            gif_ranges=global_gif_ranges,
            duration_sec=duration_sec,
        )
    frames = entry.get("frames") if entry.get("frames") is not None else global_frames
    gif_sec = (
        entry.get("gif_sec") if entry.get("gif_sec") is not None else global_gif_sec
    )
    clips = entry.get("clips") if entry.get("clips") is not None else global_clips
    gif_ranges = (
        entry.get("gif_ranges")
        if entry.get("gif_ranges") is not None
        else global_gif_ranges
    )
    return normalize_frame_opts(
        frames,
        gif_sec,
        clips,
        gif_ranges=gif_ranges,
        duration_sec=duration_sec,
    )


def compose_try_stages(
    *,
    want_translate: bool = True,
    want_notes: bool = True,
    has_media: bool = False,
    want_clips: bool | None = None,
    want_frames: bool = False,
    want_dehardsub: bool = False,
    want_deblur: bool = False,
    want_enhance: bool = False,
    want_compress: bool = False,
    want_concat: bool = False,
    want_remix: bool = False,
    want_publish: bool = False,
) -> str:
    """Map workbench toggles → comma stages (compat). Prefer expand_workflow_tasks."""
    clips = has_media if want_clips is None else bool(want_clips)
    tasks = expand_workflow_tasks(
        want_translate=want_translate,
        want_notes=want_notes,
        want_clips=clips,
        want_frames=want_frames,
        want_dehardsub=want_dehardsub,
        want_deblur=want_deblur,
        want_enhance=want_enhance,
        want_compress=want_compress,
        want_concat=want_concat,
        want_remix=want_remix,
        want_publish=want_publish,
    )
    if not tasks:
        return "notes,localize"
    if len(tasks) == 1 and tasks[0]["workflow"] == "all":
        return "all"
    # Compat preset: both text WFs → ``all`` (+ opt-in postproc).
    if want_translate and want_notes:
        post = [
            t
            for t in tasks
            if t["workflow"] not in ("translate", "notes", "frames")
        ]
        if not post and not want_frames:
            return "all"
        parts: list[str] = ["all"]
        seen = {"all"}
        for t in post:
            for p in t["stages"].split(","):
                p = p.strip()
                if p and p not in seen:
                    seen.add(p)
                    parts.append(p)
        if want_frames:
            parts.append("frames")
        return ",".join(parts)
    parts = []
    seen: set[str] = set()
    for t in tasks:
        for p in t["stages"].split(","):
            p = p.strip()
            if p and p not in seen:
                seen.add(p)
                parts.append(p)
    return ",".join(parts) if parts else "all"


def _media_workflow_fields(
    *,
    want_translate: bool,
    want_notes: bool,
    has_media: bool,
    stages: str | None = None,
    want_dehardsub: bool = False,
    want_deblur: bool = False,
    want_enhance: bool = False,
    want_compress: bool = False,
    want_concat: bool = False,
    want_remix: bool = False,
    want_publish: bool = False,
    media_opts: dict | None = None,
) -> dict:
    from media_ops import normalize_media_opts

    stage_s = (stages or "").strip() or compose_try_stages(
        want_translate=want_translate,
        want_notes=want_notes,
        has_media=has_media,
        want_dehardsub=want_dehardsub,
        want_deblur=want_deblur,
        want_enhance=want_enhance,
        want_compress=want_compress,
        want_concat=want_concat,
        want_remix=want_remix,
        want_publish=want_publish,
    )
    return {
        "want_dehardsub": bool(want_dehardsub),
        "want_deblur": bool(want_deblur),
        "want_enhance": bool(want_enhance),
        "want_compress": bool(want_compress),
        "want_concat": bool(want_concat),
        "want_remix": bool(want_remix),
        "want_publish": bool(want_publish),
        "media_opts": normalize_media_opts(media_opts),
        "stages": stage_s,
    }


def resolve_try_intent(
    *,
    job_status: str | None,
    stages: str | None = None,
    frames: str = "auto",
    has_clips: bool = False,
    has_gif_ranges: bool = False,
    new_langs: str | list | None = None,
    prev_langs: str | list | None = None,
    new_frame_opts: dict | None = None,
    prev_frame_opts: dict | None = None,
) -> dict:
    """Decide what a try submit should do.

    Priority (explicit > langs change > media tweak > noop):
      1. stages in clips|media|frames → media-only refresh
      2. new / pending / failed job → full pipeline (stages=all)
      3. done + langs changed → post (reuse ASR, re-translate + media)
      4. done + langs same + frame_opts same → noop
      5. done + frames=none + clips → clips-only
      6. done + media fields / opts changed → media refresh
    """
    new_pack = normalize_try_langs(new_langs)
    prev_pack = normalize_try_langs(prev_langs) if prev_langs is not None else None
    mode = (frames or "auto").strip().lower()
    explicit = (stages or "").strip().lower()

    if explicit in ("clips", "media", "frames", "gif", "mp4", "jpg"):
        if explicit in ("gif", "jpg"):
            explicit = "frames"
        if explicit == "mp4":
            explicit = "clips"
        return {
            "intent": explicit,
            "stages": explicit,
            "langs": new_pack,
            "reason": "explicit_stages",
        }

    st = (job_status or "").strip().lower()
    if st not in ("done", "published"):
        return {
            "intent": "full",
            "stages": "all",
            "langs": new_pack,
            "reason": "new_or_retry",
        }

    postproc = {
        p.strip()
        for p in explicit.replace(";", ",").split(",")
        if p.strip() in ("enhance", "compress", "concat", "remix", "publish")
    }
    if postproc:
        parts: list[str] = []
        if "concat" in postproc:
            parts.append("clips")
        for name in ("concat", "enhance", "compress", "remix", "publish"):
            if name in postproc:
                parts.append(name)
        return {
            "intent": "media",
            "stages": ",".join(parts),
            "langs": new_pack,
            "reason": "postproc",
        }

    if prev_pack is None or not langs_equal(prev_pack, new_pack):
        return {
            "intent": "post",
            "stages": "post",
            "langs": new_pack,
            "reason": "langs_changed",
        }

    opts_same = frame_opts_equal(prev_frame_opts, new_frame_opts)
    if opts_same:
        return {
            "intent": "noop",
            "stages": "noop",
            "langs": new_pack,
            "reason": "unchanged",
        }

    if mode == "none" and has_clips:
        return {
            "intent": "clips",
            "stages": "clips",
            "langs": new_pack,
            "reason": "clips_only",
        }

    if has_clips or has_gif_ranges or mode in ("gif", "jpg"):
        media_stages = "media"
        if has_clips and not has_gif_ranges and mode == "none":
            media_stages = "clips"
        elif (has_gif_ranges or mode in ("gif", "jpg")) and not has_clips:
            media_stages = "frames"
        return {
            "intent": media_stages,
            "stages": media_stages,
            "langs": new_pack,
            "reason": "media_refresh",
        }

    # e.g. gif_sec or frames mode flipped with no explicit ranges
    return {
        "intent": "media",
        "stages": "media",
        "langs": new_pack,
        "reason": "frame_opts_changed",
    }


def normalize_frame_opts(
    frames: str | None = "auto",
    gif_sec: float | int | None = 4.0,
    clips: list | None = None,
    *,
    gif_ranges: list | None = None,
    duration_sec: float | int | None = None,
) -> dict:
    mode = (frames or "auto").strip().lower()
    if mode not in ("auto", "none", "gif", "jpg"):
        mode = "auto"
    try:
        sec = float(gif_sec if gif_sec is not None else 4.0)
    except (TypeError, ValueError):
        sec = 4.0
    sec = max(1.0, min(20.0, sec))
    vid_dur: float | None = None
    try:
        if duration_sec is not None:
            vid_dur = float(duration_sec)
            if vid_dur <= 0:
                vid_dur = None
    except (TypeError, ValueError):
        vid_dur = None
    if vid_dur is not None:
        sec = min(sec, vid_dur)

    def _clean_spans(raw: list | None, *, max_span: float) -> list[dict]:
        out: list[dict] = []
        if not isinstance(raw, list):
            return out
        for item in raw[:24]:
            if not isinstance(item, dict):
                continue
            try:
                start = float(item.get("start"))
                end = float(item.get("end"))
            except (TypeError, ValueError):
                continue
            if end <= start:
                continue
            if start < 0:
                start = 0.0
            if vid_dur is not None:
                if start >= vid_dur:
                    continue
                end = min(end, vid_dur)
            if end - start > max_span:
                end = start + max_span
            if end <= start:
                continue
            out.append({"start": round(start, 3), "end": round(end, 3)})
        return out

    clean_gif = _clean_spans(gif_ranges, max_span=20.0)
    clean_clips = _clean_spans(clips, max_span=60.0)
    # If custom GIF ranges exist, derive gif_sec from the longest span (auto fallback unused).
    if clean_gif:
        longest = max(x["end"] - x["start"] for x in clean_gif)
        sec = max(1.0, min(20.0, longest))
    return {
        "frames": mode,
        "gif_sec": sec,
        "gif_ranges": clean_gif,
        "clips": clean_clips,
    }


def job_frame_opts(job) -> dict:
    raw = job["meta_json"] if job is not None else None
    if not raw:
        return normalize_frame_opts()
    try:
        meta = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return normalize_frame_opts()
    if not isinstance(meta, dict):
        return normalize_frame_opts()
    opts = meta.get("frame_opts") if isinstance(meta.get("frame_opts"), dict) else meta
    dur = None
    try:
        if job is not None and job["duration_sec"] is not None:
            dur = float(job["duration_sec"])
    except (TypeError, ValueError, KeyError):
        dur = None
    return normalize_frame_opts(
        opts.get("frames"),
        opts.get("gif_sec"),
        opts.get("clips"),
        gif_ranges=opts.get("gif_ranges"),
        duration_sec=dur,
    )


def clear_keypoint_images(work_dir: Path) -> int:
    """Remove step GIF/JPG fields from locale notes. Keeps custom-range MP4 rows."""
    from note_frames import _is_range_clip_row

    notes = Path(work_dir) / "notes"
    if not notes.is_dir():
        return 0
    n = 0
    for js in notes.glob("*/summary.json"):
        try:
            data = json.loads(js.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        kps = list(data.get("key_points") or [])
        changed = False
        cleaned = []
        for kp in kps:
            if not isinstance(kp, dict):
                cleaned.append(kp)
                continue
            row = dict(kp)
            # Range MP4 rows are owned by attach_video_range_clips — do not strip.
            if _is_range_clip_row(row):
                cleaned.append(row)
                continue
            # Orphan "片段 N" / "动图 N" rows left after a bad clear — drop them.
            title = str(row.get("title") or "")
            if not row.get("image") and (
                title.startswith("片段")
                or title.startswith("动图 ")
                or title.lower().startswith("clip ")
                or title.lower().startswith("gif ")
            ):
                changed = True
                continue
            if title.startswith("动图 ") or title.lower().startswith("gif "):
                # Recreated by attach_gif_ranges when custom ranges are set.
                changed = True
                continue
            if "image" in row or "start_sec" in row or "end_sec" in row:
                row.pop("image", None)
                row.pop("start_sec", None)
                row.pop("end_sec", None)
                # Study notes must not keep mp4 clip paths (GIF block only).
                row.pop("clip", None)
                changed = True
            cleaned.append(row)
        if changed:
            data["key_points"] = cleaned
            js.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            n += 1
    return n


ROOT = Path(__file__).resolve().parents[1]
WORK_ROOT = ROOT / "downloads" / "batch"
SITE_PUBLIC = ROOT.parent / "transform" / "public"
SITE_FRAMES = SITE_PUBLIC / "frames"
ARTICLES_JSON = ROOT.parent / "transform" / "content" / "articles.json"

# history 的笔记链接必须指向已导出的文章，否则点开即 404。
# articles.json 约 800KB，按 mtime 缓存，仅在导出更新时重读。
_exported_slugs_cache: tuple[float, frozenset[str]] | None = None
_exported_slugs_lock = threading.Lock()


def _exported_article_slugs() -> frozenset[str]:
    global _exported_slugs_cache
    try:
        mtime = ARTICLES_JSON.stat().st_mtime
    except OSError:
        return frozenset()
    cached = _exported_slugs_cache
    if cached is not None and cached[0] == mtime:
        return cached[1]
    with _exported_slugs_lock:
        cached = _exported_slugs_cache
        if cached is not None and cached[0] == mtime:
            return cached[1]
        try:
            payload = json.loads(ARTICLES_JSON.read_text(encoding="utf-8"))
            slugs = frozenset(
                str(a.get("slug") or "")
                for a in (payload.get("articles") or [])
                if a.get("slug")
            )
        except (OSError, ValueError, TypeError):
            slugs = frozenset()
        _exported_slugs_cache = (mtime, slugs)
        return slugs

_queue_paused = False
_cancel_requested: set[int] = set()
_STALE_SEC = 300

# Dual-lane scheduler: content (translate/notes/frames) ∥ media (dehardsub…).
# VITUAL_MAX_ACTIVE_JOBS=1 restores legacy single-flight behaviour.
_run_lock = threading.Lock()
_active_jobs: dict[str, list[int]] = {LANE_CONTENT: [], LANE_MEDIA: []}
_export_lock = threading.Lock()


def _max_active_total() -> int:
    import os

    try:
        return max(1, int(os.environ.get("VITUAL_MAX_ACTIVE_JOBS", "3")))
    except (TypeError, ValueError):
        return 3


def _lane_cap(lane: str) -> int:
    import os

    if _max_active_total() <= 1:
        return 1
    key = (
        "VITUAL_CONTENT_LANE_CAP"
        if lane == LANE_CONTENT
        else "VITUAL_MEDIA_LANE_CAP"
    )
    default = "2" if lane == LANE_CONTENT else "1"
    try:
        return max(1, int(os.environ.get(key, default)))
    except (TypeError, ValueError):
        return 2 if lane == LANE_CONTENT else 1


def _active_job_count() -> int:
    with _run_lock:
        return sum(len(v) for v in _active_jobs.values())


def _busy_flag() -> bool:
    return _active_job_count() > 0


def _primary_active_job_id() -> int | None:
    with _run_lock:
        for lane in (LANE_CONTENT, LANE_MEDIA):
            if _active_jobs[lane]:
                return int(_active_jobs[lane][0])
    return None


def _active_by_lane_snapshot() -> dict[str, list[int]]:
    with _run_lock:
        return {k: list(v) for k, v in _active_jobs.items()}


def _lane_for_job(job) -> str:
    meta = _job_meta_dict(job)
    lane = str(meta.get("lane") or "").strip()
    if lane in (LANE_CONTENT, LANE_MEDIA):
        return lane
    return lane_for_workflow(_job_workflow(job))


def _try_acquire_slot(lane: str, job_id: int) -> bool:
    """Reserve a lane slot. Returns True only when *this* call appended the id.

    A second wake for the same job_id must get False — otherwise two workers
    both think they own the slot and the loser's ``finally`` releases the
    winner mid-``process_job``.
    """
    with _run_lock:
        if int(job_id) in _active_jobs[lane]:
            return False
        total = sum(len(v) for v in _active_jobs.values())
        if _max_active_total() <= 1:
            if total >= 1:
                return False
        else:
            if len(_active_jobs[lane]) >= _lane_cap(lane):
                return False
            if total >= _max_active_total():
                return False
        _active_jobs[lane].append(int(job_id))
        return True


def _release_slot(lane: str, job_id: int) -> None:
    with _run_lock:
        try:
            _active_jobs[lane].remove(int(job_id))
        except ValueError:
            pass


def _is_active_job(job_id: int | None) -> bool:
    if job_id is None:
        return False
    jid = int(job_id)
    with _run_lock:
        return any(jid in ids for ids in _active_jobs.values())


def _work_dir(platform: str, video_id: str) -> Path:
    return WORK_ROOT / f"{platform}_{video_id}"


def _file_api_url(job_id: int, rel: str) -> str:
    from urllib.parse import quote

    return f"/api/try/{int(job_id)}/file?rel={quote(rel, safe='/')}"


def keypoint_clips_for_job(job_id: int, *, max_points: int = 12) -> dict:
    """Return clip ranges derived from notes key_points for a try job."""
    from note_frames import keypoint_clip_ranges

    queue = QueueDB()
    try:
        job = queue.get_job(int(job_id))
        if job is None:
            return {"ok": False, "error": "job not found"}
        platform = job["platform"]
        video_id = job["video_id"]
        work = _work_dir(platform, video_id)
        try:
            clips = keypoint_clip_ranges(work, max_points=max_points)
        except FileNotFoundError as e:
            return {"ok": False, "error": str(e), "clips": []}
        return {
            "ok": True,
            "job_id": int(job_id),
            "pack_id": _job_pack_id(job, platform, video_id),
            "platform": platform,
            "video_id": video_id,
            "clips": clips,
            "count": len(clips),
        }
    finally:
        queue.close()


def resolve_try_pack_file(job_id: int, rel: str) -> tuple[Path, str, str]:
    """Resolve a safe file under the job's pack work_dir."""
    from job_pack import safe_resolve_under

    queue = QueueDB()
    try:
        job = queue.get_job(int(job_id))
        if job is None:
            raise FileNotFoundError("job not found")
        work = _work_dir(job["platform"], job["video_id"])
        path = safe_resolve_under(work, rel)
    finally:
        queue.close()
    suffix = path.suffix.lower()
    media = {
        ".srt": "application/x-subrip",
        ".json": "application/json",
        ".md": "text/markdown; charset=utf-8",
        ".txt": "text/plain; charset=utf-8",
        ".mp4": "video/mp4",
        ".vtt": "text/vtt",
        ".wav": "audio/wav",
    }.get(suffix, "application/octet-stream")
    return path, media, path.name


def build_try_pack_zip(job_id: int) -> tuple[bytes, str]:
    """Build Job Pack zip bytes + download filename."""
    from job_pack import build_job_pack_bytes

    queue = QueueDB()
    try:
        job = queue.get_job(int(job_id))
        if job is None:
            raise FileNotFoundError("job not found")
        platform = job["platform"]
        video_id = job["video_id"]
        pack = _job_pack_id(job, platform, video_id)
        work = _work_dir(platform, video_id)
        if not work.is_dir():
            raise FileNotFoundError(str(work))
        data = build_job_pack_bytes(
            work,
            pack_id=pack,
            meta_extra={
                "job_id": int(job_id),
                "platform": platform,
                "video_id": video_id,
                "workflow": _job_workflow(job),
            },
        )
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in pack)[:80]
        return data, f"{safe or 'job'}_pack.zip"
    finally:
        queue.close()


# infer_try_progress / stale reaper / history 都会调用 _latest_mtime，
# 前端 3~5s 轮询一次。短 TTL 缓存避免每次递归 stat 整个工作目录（含视频/frames）。
_latest_mtime_cache: dict[str, tuple[float, float | None]] = {}
_LATEST_MTIME_TTL = 2.0


def _latest_mtime(root: Path) -> float | None:
    key = str(root)
    now = time.monotonic()
    cached = _latest_mtime_cache.get(key)
    if cached is not None and now - cached[0] < _LATEST_MTIME_TTL:
        return cached[1]
    if not root.is_dir():
        _latest_mtime_cache[key] = (now, None)
        return None
    latest: float | None = None
    for p in root.rglob("*"):
        if p.is_file():
            try:
                mt = p.stat().st_mtime
            except OSError:
                continue
            if latest is None or mt > latest:
                latest = mt
    _latest_mtime_cache[key] = (now, latest)
    return latest


def infer_work_source_lang(work: Path, srts: dict[str, Path]) -> str:
    """Prefer notes.source_lang, then sole SRT locale, then zh/en heuristic."""
    notes = work / "notes"
    if notes.is_dir():
        for js in sorted(notes.glob("*/summary.json")):
            try:
                data = json.loads(js.read_text(encoding="utf-8-sig"))
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                data = {}
            raw = ""
            if isinstance(data, dict):
                raw = str(data.get("source_lang") or data.get("summary_lang") or "")
            tag = normalize_lang(raw) if raw else normalize_lang(js.parent.name)
            if tag:
                return coalesce_source_lang(tag)
    if len(srts) == 1:
        return coalesce_source_lang(next(iter(srts)))
    if "en" in srts and "zh" not in srts:
        return "en"
    if "zh" in srts:
        return "zh"
    if srts:
        return coalesce_source_lang(next(iter(srts)))
    return "en"


def _eta_sec_from_progress(progress: dict | None, created_at: object) -> int | None:
    """Linear ETA from elapsed / percent. None when too early or terminal.

    Skips absurd ETAs from stale ``created_at`` (requeue before created_at reset).
    """
    if not isinstance(progress, dict):
        return None
    stage = str(progress.get("stage") or "")
    if stage in ("queued", "done", "failed", "cancelled", "dead"):
        return None
    try:
        pct = float(progress.get("percent") or 0)
        started = float(created_at)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if pct < 5.0 or started <= 0:
        return None
    elapsed = time.time() - started
    if elapsed < 8.0:
        return None
    # >6h clock with mid progress almost always means reused created_at.
    if elapsed > 6 * 3600 and pct < 95.0:
        return None
    eta = int(elapsed * (100.0 - min(pct, 99.5)) / pct)
    if eta > 12 * 3600:
        return None
    return max(0, eta)


def _with_eta(progress: dict | None, created_at: object) -> dict | None:
    if not isinstance(progress, dict):
        return progress
    out = dict(progress)
    out["eta_sec"] = _eta_sec_from_progress(out, created_at)
    return out


def infer_try_progress(
    platform: str,
    video_id: str,
    status: str,
    *,
    langs: str = DEFAULT_PACK,
    job_id: int | None = None,
) -> dict:
    """Best-effort progress from work-dir artifacts (no pipeline hooks needed)."""
    work = _work_dir(platform, video_id)
    now = time.time()
    latest = _latest_mtime(work)
    updated_sec_ago = int(now - latest) if latest else None
    stale = (
        status == "processing"
        and latest is not None
        and (now - latest) >= _STALE_SEC
    )
    active = status == "processing" and latest is not None and not stale

    if status in ("done", "published"):
        return {
            "percent": 100,
            "stage": "done",
            "detail": None,
            "active": False,
            "stale": False,
            "updated_sec_ago": int(now - latest) if latest else None,
        }
    if status in ("failed", "dead"):
        return {
            "percent": 0,
            "stage": "failed",
            "detail": None,
            "active": False,
            "stale": False,
            "updated_sec_ago": int(now - latest) if latest else None,
        }
    if status == "cancelled":
        return {
            "percent": 0,
            "stage": "cancelled",
            "detail": None,
            "active": False,
            "stale": False,
            "updated_sec_ago": int(now - latest) if latest else None,
        }
    if status == "pending":
        # Prefer schedule-aware queued hint only when this job's lane is full
        # or deps block it — not merely because the other lane is busy.
        if job_id is not None and not _is_active_job(job_id):
            try:
                q = QueueDB()
                try:
                    row = q.get_job(int(job_id))
                    if row is not None:
                        hint = _schedule_hint(q, row)
                        if hint.get("waiting_for_depends_on") or hint.get("lane_full"):
                            detail = hint.get("blocked_on")
                            return {
                                "percent": 0,
                                "stage": "queued",
                                "detail": detail,
                                "active": False,
                                "stale": False,
                                "updated_sec_ago": updated_sec_ago,
                            }
                finally:
                    q.close()
            except Exception:
                if _busy_flag():
                    return {
                        "percent": 0,
                        "stage": "queued",
                        "detail": None,
                        "active": False,
                        "stale": False,
                        "updated_sec_ago": updated_sec_ago,
                    }
        media = work / "media"
        src_mp4 = media / "source.mp4"
        has_activity = latest is not None and (now - latest) < _STALE_SEC
        if has_activity or (src_mp4.is_file() and src_mp4.stat().st_size > 0):
            percent = 8
            if src_mp4.is_file():
                try:
                    sz = src_mp4.stat().st_size
                    percent = min(12 + int(min(sz, 80_000_000) / 80_000_000 * 12), 24)
                except OSError:
                    pass
            elif (media / "full_16k.wav").is_file() or existing_wav(work):
                percent = 14
            return {
                "percent": percent,
                "stage": "download",
                "detail": None,
                "active": True,
                "stale": False,
                "updated_sec_ago": int(now - latest) if latest else 0,
            }
        return {
            "percent": 0,
            "stage": "queued",
            "detail": None,
            "active": False,
            "stale": False,
            "updated_sec_ago": int(now - latest) if latest else None,
        }

    wav = existing_wav(work)
    srts = list_locale_srts(work) if work.is_dir() else {}
    pack = normalize_try_langs(langs)
    src_lang = infer_work_source_lang(work, srts)
    targets = resolve_targets(pack, src_lang)
    translated = [t for t in srts if t != src_lang]
    notes_dir = work / "notes"
    note_langs = (
        sorted(p.parent.name for p in notes_dir.glob("*/summary.json"))
        if notes_dir.is_dir()
        else []
    )
    src_notes = notes_dir / src_lang / "summary.json"
    localized = [t for t in note_langs if t != src_lang]
    slug_guess = video_id.lower()
    frame_dirs = [
        work / "media" / "frames",
        SITE_FRAMES / slug_guess,
    ]
    has_frames = any(d.is_dir() and any(d.glob("*")) for d in frame_dirs)

    percent = 2
    stage = "download"
    detail: str | None = None
    translate_total = max(len(targets), 1)
    translate_done = len(translated)
    translate_complete = (
        translate_done >= len(targets) if targets else bool(srts.get(src_lang))
    )

    if wav and wav.is_file():
        # Download finished; ASR can take a long time before any .srt appears.
        percent = 15
        stage = "transcribe"
    if srts.get(src_lang):
        percent = 38
        stage = "transcribe"
    if translated:
        stage = "translate"
        percent = 38 + int(32 * translate_done / translate_total)
        detail = f"{translate_done}/{translate_total}"
    # Translate done but summary not written yet — LLM notes can run many minutes silently.
    if translate_complete and not src_notes.is_file():
        stage = "notes"
        percent = max(percent, 71)
        detail = None
    # Notes often run parallel with translate — advance stage only when captions caught up.
    if src_notes.is_file() and translate_complete:
        percent = max(percent, 72)
        stage = "notes"
        detail = None
    if localized and translate_complete:
        stage = "localize"
        total = max(len(targets), 1)
        done = len(localized)
        percent = 72 + int(18 * min(done, total) / total)
        detail = f"{done}/{total}"
    if has_frames:
        percent = max(percent, 94)
        stage = "gif"
        detail = None

    concat_mp4 = work / "media" / "concat" / "concat.mp4"
    enhanced_mp4 = work / "media" / "enhance" / "enhanced.mp4"
    compressed_mp4 = work / "media" / "compress" / "compressed.mp4"
    remix_mp4 = work / "media" / "remix" / "remix.mp4"
    if concat_mp4.is_file():
        percent = max(percent, 95)
        stage = "concat"
        detail = None
    if enhanced_mp4.is_file():
        percent = max(percent, 96)
        stage = "enhance"
        detail = None
    if compressed_mp4.is_file():
        percent = max(percent, 97)
        stage = "compress"
        detail = None
    if remix_mp4.is_file():
        percent = max(percent, 98)
        stage = "remix"
        detail = None
    publish_report = work / "media" / "publish" / "report.json"
    if publish_report.is_file():
        percent = max(percent, 99)
        stage = "publish"
        detail = None

    if status == "processing" and percent >= 94 and not has_frames and src_notes.is_file():
        stage = "gif"
        percent = max(percent, 92)

    return {
        "percent": min(percent, 99) if status == "processing" else percent,
        "stage": stage,
        "detail": detail,
        "active": active,
        "stale": stale,
        "updated_sec_ago": updated_sec_ago,
    }


def try_status() -> dict:
    by_lane = _active_by_lane_snapshot()
    return {
        "busy": _busy_flag(),
        "paused": _queue_paused,
        "active_job_id": _primary_active_job_id(),
        "active_job_ids": by_lane,
        "max_active_jobs": _max_active_total(),
    }


def pause_try_queue() -> dict:
    global _queue_paused
    _queue_paused = True
    return {"ok": True, **try_status()}


def resume_try_queue() -> dict:
    global _queue_paused
    _queue_paused = False
    schedule_next_try_job()
    return {"ok": True, **try_status()}


def cancel_try_job(job_id: int) -> dict:
    """Cancel a try job and its unfinished pack siblings.

    Dual-lane packs leave several pending WFs; cancelling only the clicked
    row used to orphan notes/media forever. Cancel every pending/processing
    sibling for the same (platform, video_id), request interrupt on any that
    are running, then kick the scheduler so other packs can fill free slots.
    """
    queue = QueueDB()
    cancelled_ids: list[int] = []
    cancelling = False
    try:
        job = queue.get_job(job_id)
        if job is None:
            return {"ok": False, "error": "job not found", "job_id": job_id}
        st = str(job["status"] or "")
        if st in ("done", "published", "cancelled"):
            return {"ok": False, "error": f"job is {st}", "job_id": job_id}

        platform = str(job["platform"] or "")
        video_id = str(job["video_id"] or "")
        siblings = queue.list_video_jobs(platform, video_id) if platform and video_id else [job]
        for row in siblings:
            sid = int(row["id"])
            sst = str(row["status"] or "")
            if sst in ("done", "published", "cancelled", "failed", "dead"):
                # Allow explicit cancel of a failed/dead primary row.
                if sid == job_id and sst in ("failed", "dead"):
                    if queue.cancel_job(sid, reason="用户取消"):
                        cancelled_ids.append(sid)
                continue
            if sst == "processing":
                _cancel_requested.add(sid)
                cancelling = True
            if queue.cancel_job(sid, reason="用户取消" if sst == "pending" else "用户已取消"):
                cancelled_ids.append(sid)

        if not cancelled_ids:
            return {"ok": False, "error": "could not cancel", "job_id": job_id}

        snap = job_snapshot(job_id)
        snap["ok"] = True
        snap["cancelled_job_ids"] = cancelled_ids
        if cancelling:
            snap["cancelling"] = True
        return snap
    finally:
        queue.close()
        # Free dual-lane slots for other packs.
        kick_try_scheduler()


def retry_try_job(job_id: int) -> dict:
    """Re-queue a failed/cancelled try job and start when idle.

    Pack cancel fans out to siblings; retry mirrors that for cancelled/failed
    siblings on the same (platform, video_id) so notes/media are not orphaned.
    """
    if _is_active_job(int(job_id)):
        return {
            "ok": False,
            "error": "job still holds a lane slot — wait for worker exit",
            "job_id": job_id,
        }
    queue = QueueDB()
    try:
        job = queue.get_job(job_id)
        if job is None:
            return {"ok": False, "error": "job not found", "job_id": job_id}
        st = str(job["status"] or "")
        if st == "processing":
            return {"ok": False, "error": "job is running", "job_id": job_id}
        if st in ("done", "published"):
            return {"ok": False, "error": f"job is {st}", "job_id": job_id}

        platform = str(job["platform"] or "")
        video_id = str(job["video_id"] or "")
        siblings = (
            queue.list_video_jobs(platform, video_id) if platform and video_id else [job]
        )
        # Refuse partial pack reopen while any terminal sibling still holds a
        # lane — otherwise dependents become pending forever behind cancelled.
        winding = [
            int(r["id"])
            for r in siblings
            if str(r["status"] or "") in ("failed", "dead", "cancelled", "partial")
            and _is_active_job(int(r["id"]))
        ]
        if winding:
            return {
                "ok": False,
                "error": "pack sibling still winding down — wait for worker exit",
                "job_id": job_id,
                "active_job_ids": winding,
            }
        requeued_ids: list[int] = []
        for row in siblings:
            sid = int(row["id"])
            sst = str(row["status"] or "")
            if sst not in ("failed", "dead", "cancelled", "partial"):
                continue
            if not queue.requeue_job(sid):
                continue
            _cancel_requested.discard(sid)
            requeued_ids.append(sid)
            sjob = queue.get_job(sid) or row
            meta = _job_meta_dict(sjob)
            stages = str(meta.get("stages") or "")
            if "asr" in {p.strip() for p in stages.split(",") if p.strip()}:
                queue.set_meta(sid, {"depends_on_artifacts": []})

        if int(job_id) not in requeued_ids:
            if queue.requeue_job(job_id):
                _cancel_requested.discard(int(job_id))
                requeued_ids.append(int(job_id))
                meta = _job_meta_dict(queue.get_job(job_id) or job)
                stages = str(meta.get("stages") or "")
                if "asr" in {p.strip() for p in stages.split(",") if p.strip()}:
                    queue.set_meta(int(job_id), {"depends_on_artifacts": []})
            elif not requeued_ids:
                return {"ok": False, "error": "could not requeue", "job_id": job_id}

        job = queue.get_job(job_id) or job
        blocked = not _deps_satisfied(queue, job)
        snap = job_snapshot(job_id)
        snap["ok"] = True
        snap["requeued_job_ids"] = requeued_ids
        if blocked:
            snap["waiting_for_depends_on"] = True
            snap["depends_on"] = list(_job_meta_dict(job).get("depends_on") or [])
            snap["depends_on_artifacts"] = list(
                _job_meta_dict(job).get("depends_on_artifacts") or []
            )
        return snap
    finally:
        queue.close()
        kick_try_scheduler()


def group_action_try_jobs(job_ids: list[int], action: str) -> dict:
    """Delete or re-run a batch of finished jobs (one history group in the UI)."""
    ids = sorted(
        {
            int(i)
            for i in (job_ids or [])
            if isinstance(i, (int, float)) and int(i) > 0
        }
    )
    if not ids or len(ids) > 100:
        return {"ok": False, "error": "invalid job selection"}
    if action not in ("delete", "retry"):
        return {"ok": False, "error": "unknown action"}

    queue = QueueDB()
    terminal = ("done", "published", "failed", "dead", "cancelled", "partial")
    try:
        rows = []
        for jid in ids:
            row = queue.get_job(jid)
            if row is not None:
                rows.append(row)
        if not rows:
            return {"ok": False, "error": "任务不存在"}

        targets = rows
        if action == "delete":
            # Deleting an upstream workflow would leave downstream jobs waiting
            # on a dependency that can never be satisfied. Cascade to them.
            # sqlite3.Row has no .get() — always index columns.
            by_id = {int(r["id"]): r for r in rows}
            for pack in {str(r["pack_id"] or "") for r in rows if r["pack_id"]}:
                for sibling in queue.list_pack_jobs(pack):
                    by_id.setdefault(int(sibling["id"]), sibling)
            target_ids = {int(r["id"]) for r in targets}
            queue_ids = [int(r["id"]) for r in rows]
            while queue_ids:
                cur = queue_ids.pop()
                cur_row = by_id.get(cur)
                if cur_row is None:
                    continue
                cur_wf = str(cur_row["workflow"] or "")
                if not cur_wf:
                    continue
                for sid, sibling in by_id.items():
                    if sid in queue_ids or sid == cur or sid in target_ids:
                        continue
                    deps = _job_meta_dict(sibling).get("depends_on") or []
                    if cur_wf in deps:
                        targets.append(sibling)
                        target_ids.add(sid)
                        queue_ids.append(sid)
            targets = [by_id[i] for i in target_ids if i in by_id]

        active = [
            int(r["id"])
            for r in targets
            if _is_active_job(int(r["id"]))
            or str(r["status"] or "") in ("processing", "pending", "awaiting_confirm")
        ]
        if active:
            return {
                "ok": False,
                "error": "有任务仍在进行中，请稍后再试",
                "active_job_ids": active,
            }

        changed: list[int] = []
        if action == "delete":
            for row in targets:
                jid = int(row["id"])
                if queue.delete_job(jid, allow_statuses=terminal):
                    changed.append(jid)
        else:
            for row in targets:
                jid = int(row["id"])
                if not queue.requeue_job(jid, allow_done=True):
                    continue
                _cancel_requested.discard(jid)
                changed.append(jid)
                meta = _job_meta_dict(queue.get_job(jid) or row)
                stages = {p.strip() for p in str(meta.get("stages") or "").split(",") if p.strip()}
                if "asr" in stages:
                    queue.set_meta(jid, {"depends_on_artifacts": []})

        if not changed:
            return {"ok": False, "error": "没有可操作的任务"}
        return {"ok": True, "action": action, "job_ids": changed, "count": len(changed)}
    finally:
        queue.close()
        if action == "retry":
            kick_try_scheduler()


def reap_stale_try_jobs(*, stale_sec: int = _STALE_SEC) -> int:
    """Mark orphaned processing try jobs as failed after API restart or timeout.

    Skip only jobs that still hold an in-process lane slot — do not bail out
    when *another* lane is busy (legacy single-flight guard).
    """
    queue = QueueDB()
    reaped = 0
    try:
        rows = queue._conn.execute(
            """
            SELECT id, platform, video_id FROM jobs
            WHERE status='processing' AND priority='high'
            """
        ).fetchall()
        now = time.time()
        for row in rows:
            jid = int(row["id"])
            if _is_active_job(jid):
                continue
            platform = row["platform"]
            video_id = row["video_id"]
            latest = _latest_mtime(_work_dir(platform, video_id))
            if latest is not None and (now - latest) < stale_sec:
                continue
            job = queue.get_job(jid)
            wf = _job_workflow(job) if job else ""
            queue.mark_failed(
                jid,
                "任务已中断（后台重启或超时）。请重新提交。",
            )
            _cascade_fail_dependents(
                queue,
                platform,
                video_id,
                wf,
                "依赖任务已中断（后台重启或超时）。请重新提交。",
            )
            reaped += 1
            print(f"[try] reaped stale job#{jid} {platform}:{video_id}")
    finally:
        queue.close()
    return reaped


def list_active_try_jobs(*, limit: int = 10) -> dict:
    """Pending/processing high-priority try jobs (for workbench resume)."""
    reap_stale_try_jobs()
    queue = QueueDB()
    try:
        queue.reset_running_steps_to_queued()
        rows = queue._conn.execute(
            """
            SELECT id FROM jobs
            WHERE status IN ('pending', 'processing', 'partial') AND priority='high'
            ORDER BY CASE status WHEN 'processing' THEN 0 ELSE 1 END, updated_at DESC
            LIMIT ?
            """,
            (max(1, min(int(limit), 50)),),
        ).fetchall()
        jobs = []
        for row in rows:
            snap = job_snapshot(int(row["id"]))
            if snap.get("ok"):
                jobs.append(snap)
        return {
            "ok": True,
            "jobs": jobs,
            "count": len(jobs),
            **try_status(),
        }
    finally:
        queue.close()


def list_try_job_history(*, limit: int = 30) -> dict:
    """Recent jobs across all statuses for the workbench history list.

    Read-only: unlike ``list_active_try_jobs`` this does NOT call the stale
    reaper / step-reset, so manual CLI jobs (priority=normal, status=processing)
    are never mis-marked as failed just by viewing the history. Progress is
    inferred from on-disk products (same source as the live poll) so in-flight
    dehardsub / run_postproc jobs show a % and stage.
    """
    queue = QueueDB()
    content: ContentDB | None = None
    try:
        content = ContentDB()
        exported_slugs = _exported_article_slugs()
        rows = queue.list_jobs(limit=max(1, min(int(limit), 100)))
        jobs: list[dict] = []
        now = time.time()
        for row in rows:
            jid = int(row["id"])
            st = str(row["status"] or "pending")
            wf = _job_workflow(row)
            platform = str(row["platform"] or "")
            video_id = str(row["video_id"] or "")
            work = _work_dir(platform, video_id)
            title = _source_title(platform, video_id, str(row["title"] or ""), content)
            if not title:
                prefix = {
                    "cancelled": "已取消任务",
                    "failed": "失败任务",
                    "dead": "失败任务",
                    "partial": "部分完成任务",
                }.get(st, "任务")
                title = f"{prefix} · {video_id}" if video_id else f"{prefix} #{jid}"

            progress: dict | None = None
            if st in ("done", "published"):
                progress = {"percent": 100, "stage": "done", "active": False, "stale": False, "updated_sec_ago": None}
            elif st in ("failed", "dead", "cancelled"):
                progress = {"percent": 0, "stage": st, "active": False, "stale": True, "updated_sec_ago": None}
            elif wf and wf != "all":
                progress = infer_workflow_progress(wf, st, work, active=False)
            elif st in ("pending", "processing", "partial"):
                progress = infer_try_progress(
                    platform, video_id, st, langs=job_langs(row), job_id=jid
                )

            last_error = row["last_error"]
            # 终态任务带上笔记链接，UI 的「已完成」列表无需展开即可打开。
            path = None
            if st in ("done", "published"):
                try:
                    article = _article_for_job(content, platform, video_id)
                    # 只给已导出的文章生成链接，避免点开 404。
                    if article and article["slug"] in exported_slugs:
                        path = f"/topics/{article['topic']}/{article['slug']}"
                except Exception:
                    path = None
            jobs.append(
                {
                    "job_id": jid,
                    "status": st,
                    "workflow": wf,
                    "title": title,
                    "platform": platform,
                    "video_id": video_id,
                    "priority": str(row["priority"] or "normal"),
                    "pack_id": str(row["pack_id"] or "") or None,
                    "error": str(last_error) if last_error else None,
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "updated_sec_ago": max(0, int(now - float(row["updated_at"] or now))),
                    "progress": _with_eta(progress, row["created_at"]),
                    "path": path,
                }
            )
        return {"ok": True, "jobs": jobs, "count": len(jobs)}
    finally:
        queue.close()
        if content is not None:
            content.close()


def list_try_job_steps(job_id: int) -> dict:
    queue = QueueDB()
    try:
        job = queue.get_job(int(job_id))
        if job is None:
            return {"ok": False, "error": "job not found", "job_id": job_id}
        rows = queue.list_steps(int(job_id))
        steps = []
        for r in rows:
            steps.append(
                {
                    "step_key": r["step_key"],
                    "kind": r["kind"],
                    "lang": r["lang"],
                    "status": r["status"],
                    "attempts": int(r["attempts"] or 0),
                    "qa_status": r["qa_status"],
                    "error": r["error"],
                    "artifact_rel": r["artifact_rel"],
                    "started_at": r["started_at"],
                    "finished_at": r["finished_at"],
                }
            )
        summary = queue.steps_summary(int(job_id))
        return {
            "ok": True,
            "job_id": int(job_id),
            "workflow": _job_workflow(job),
            "job_status": job["status"],
            "steps": steps,
            "summary": summary,
        }
    finally:
        queue.close()


def retry_try_job_step(job_id: int, step_key: str) -> dict:
    """Re-run one language step when the parent job is not processing."""
    key = (step_key or "").strip()
    if not key:
        return {"ok": False, "error": "step_key required", "job_id": job_id}
    if key == "notes.source" or key.startswith("notes.source"):
        return {
            "ok": False,
            "error": "notes.source retry not supported — use job retry",
            "job_id": job_id,
        }
    lane = LANE_CONTENT
    # Acquire first — never leave a queued orphan step if the lane is busy.
    if not _try_acquire_slot(lane, int(job_id)):
        return {
            "ok": False,
            "error": "content lane busy — retry later",
            "job_id": job_id,
        }
    queue = QueueDB()
    prev_status = "done"
    try:
        job = queue.get_job(int(job_id))
        if job is None:
            _release_slot(lane, int(job_id))
            return {"ok": False, "error": "job not found", "job_id": job_id}
        st = str(job["status"] or "")
        prev_status = st
        if st in ("cancelled", "processing", "pending"):
            _release_slot(lane, int(job_id))
            return {
                "ok": False,
                "error": f"step retry not allowed while job is {st}",
                "job_id": job_id,
            }
        if st not in ("done", "published", "partial", "failed"):
            _release_slot(lane, int(job_id))
            return {
                "ok": False,
                "error": f"step retry not allowed for status {st}",
                "job_id": job_id,
            }
        step = queue.get_step(int(job_id), key)
        if step is None:
            _release_slot(lane, int(job_id))
            return {"ok": False, "error": f"step not found: {key}", "job_id": job_id}
        kind = str(step["kind"] or "")
        if kind == "notes_source":
            _release_slot(lane, int(job_id))
            return {
                "ok": False,
                "error": "notes.source retry not supported — use job retry",
                "job_id": job_id,
            }
        if not queue.requeue_step(int(job_id), key):
            _release_slot(lane, int(job_id))
            return {"ok": False, "error": "could not requeue step", "job_id": job_id}
        cur = queue._conn.execute(
            """
            UPDATE jobs SET status='processing', last_error=NULL, updated_at=?
            WHERE id=? AND status IN ('done', 'published', 'partial', 'failed')
            """,
            (time.time(), int(job_id)),
        )
        queue._conn.commit()
        if cur.rowcount != 1:
            _release_slot(lane, int(job_id))
            return {
                "ok": False,
                "error": "could not claim job for step retry",
                "job_id": job_id,
            }
    except Exception:
        _release_slot(lane, int(job_id))
        raise
    finally:
        queue.close()

    def _runner() -> None:
        try:
            _execute_step_retry(int(job_id), key, prev_status=prev_status)
        finally:
            _release_slot(lane, int(job_id))
            schedule_next_try_job(skip_job_id=int(job_id))

    threading.Thread(target=_runner, daemon=True).start()
    return {
        "ok": True,
        "job_id": int(job_id),
        "step_key": key,
        "status": "processing",
        **list_try_job_steps(int(job_id)),
    }


def _execute_step_retry(
    job_id: int, step_key: str, *, prev_status: str = "done"
) -> None:
    """Run one translate.* or notes.localize.* step with force=True."""
    from types import SimpleNamespace

    from discover.run_batch import (
        _load_existing_glossary,
        _load_existing_job_segments,
        translate_summary_locale,
    )
    from job_layout import job_media_dir
    from langs import coalesce_source_lang, file_tag
    from llm.factory import configure_llm
    from pipeline import (
        find_ollama,
        load_notes_summary,
        locked_print,
        translate_one_lang,
        write_locale_notes,
        write_notes_index,
    )

    configure_llm(profile="local")
    queue = QueueDB()
    try:
        job = queue.get_job(job_id)
        if job is None:
            return
        if str(job["status"] or "") == "cancelled" or job_id in _cancel_requested:
            _apply_cancel(queue, job_id)
            return
        step = queue.get_step(job_id, step_key)
        if step is None:
            return
        platform = job["platform"]
        video_id = job["video_id"]
        work = _work_dir(platform, video_id)
        kind = str(step["kind"] or "")
        lang = str(step["lang"] or "").strip()
        if not lang and "." in step_key:
            lang = step_key.rsplit(".", 1)[-1]

        if kind == "translate" or step_key.startswith("translate."):
            segs, out_dir, stem, src_lang, _src = _load_existing_job_segments(
                work, source_lang="auto"
            )
            glossary = _load_existing_glossary(out_dir)
            ollama = find_ollama()
            langs_pack = job_langs(job)
            args = SimpleNamespace(
                ollama_model="translategemma:4b",
                source_lang=src_lang,
                langs=langs_pack,
                target_lang=None,
                force=True,
            )
            # Force re-translate this lang only.
            srt = None
            try:
                from job_layout import locale_srt_path

                srt = locale_srt_path(out_dir, lang, stem)
                if srt.is_file():
                    srt.unlink()
            except OSError:
                pass
            translate_one_lang(
                args,
                ollama,
                segs,
                out_dir,
                stem,
                glossary,
                lang,
                force=True,
                job_id=job_id,
                queue=queue,
            )
        elif kind == "notes_localize" or step_key.startswith("notes.localize."):
            src_lang = coalesce_source_lang("auto")
            summary = load_notes_summary(work, "full_16k", src_lang)
            if not summary:
                # Try detect from existing notes.
                summary = load_notes_summary(work, "full_16k", "zh")
            if not summary:
                queue.mark_step_failed(job_id, step_key, "source notes missing")
            else:
                ollama = find_ollama()
                queue.mark_step_running(job_id, step_key)
                try:
                    proofread: dict = {}
                    loc_data = translate_summary_locale(
                        ollama,
                        "translategemma:4b",
                        summary,
                        source_lang=str(summary.get("summary_lang") or "zh"),
                        target_lang=lang,
                        proofread_report=proofread,
                    )
                    loc_data["source_lang"] = loc_data.get("source_lang") or "zh"
                    loc_data["summary_lang"] = lang
                    _js, md = write_locale_notes(loc_data, work, lang)
                    tag = file_tag(lang)
                    qa_path = job_media_dir(work) / f"notes_qa_{tag}.json"
                    qa_path.write_text(
                        json.dumps(
                            {"proofread": proofread}, ensure_ascii=False, indent=2
                        ),
                        encoding="utf-8",
                    )
                    try:
                        art = str(Path(md).relative_to(work)).replace("\\", "/")
                    except ValueError:
                        art = f"notes/{tag}.md"
                    qa_status = (
                        "needs_review"
                        if proofread.get("human_review", 0)
                        or proofread.get("status")
                        not in ("ok", "disabled", None, "")
                        else "ok"
                    )
                    if proofread.get("status") in (None, "", "disabled"):
                        qa_status = "ok"
                    queue.mark_step_done(
                        job_id,
                        step_key,
                        status="done",
                        qa_status=qa_status,
                        artifact_rel=art,
                    )
                    try:
                        write_notes_index(work)
                    except Exception:
                        pass
                    locked_print(f"[step-retry] {step_key} -> {md}")
                except BaseException as e:
                    queue.mark_step_failed(
                        job_id, step_key, f"{type(e).__name__}: {e}"
                    )
        else:
            queue.mark_step_failed(job_id, step_key, f"unsupported step kind: {kind}")

        agg = queue.aggregate_job_from_steps(job_id)
        if agg is None:
            # Step-retry must not leave parent processing forever when other
            # langs remain queued (those are not auto-started by this path).
            rows = queue.list_steps(job_id)
            terminal = [
                r
                for r in rows
                if str(r["status"] or "")
                not in ("queued", "running")
            ]
            if terminal and any(str(r["status"]) == "failed" for r in terminal):
                bad = [
                    str(r["step_key"])
                    for r in terminal
                    if str(r["status"]) == "failed"
                ]
                queue.mark_partial(
                    job_id, error=f"partial: {', '.join(bad[:12])}" if bad else None
                )
            elif prev_status == "published":
                queue._conn.execute(
                    "UPDATE jobs SET status='published', updated_at=? WHERE id=? AND status='processing'",
                    (time.time(), int(job_id)),
                )
                queue._conn.commit()
            elif prev_status == "partial":
                queue.mark_partial(job_id)
            else:
                queue.mark_done(job_id)
        with _export_lock:
            try:
                from discover.export_site import main as export_main

                export_main([])
            except Exception as e:
                print(f"[step-retry] export skipped ({type(e).__name__}: {e})")
    finally:
        queue.close()


def _job_id(
    queue: QueueDB,
    platform: str,
    video_id: str,
    workflow: str | None = None,
) -> int | None:
    if workflow:
        row = queue.get_job_by_workflow(platform, video_id, workflow)
        return int(row["id"]) if row else None
    row = queue._conn.execute(
        """
        SELECT id FROM jobs
        WHERE platform=? AND video_id=?
        ORDER BY id DESC LIMIT 1
        """,
        (platform, video_id),
    ).fetchone()
    return int(row["id"]) if row else None


def _job_workflow(job) -> str:
    try:
        wf = job["workflow"]
    except (KeyError, IndexError, TypeError):
        wf = None
    return str(wf or "all")


def _job_pack_id(job, platform: str, video_id: str) -> str:
    try:
        pid = job["pack_id"]
    except (KeyError, IndexError, TypeError):
        pid = None
    return str(pid or pack_id_for(platform, video_id))


def _upsert_workflow_job(
    queue: QueueDB,
    c: Candidate,
    *,
    workflow: str,
    pack_id: str,
    force_requeue: bool = False,
    meta: dict | None = None,
) -> tuple[int | None, str]:
    """Insert or reopen one independent workflow job. Returns (job_id, enqueue).

    ``meta`` (depends_on / stages / lane) is written before the row becomes
    claimable pending whenever we insert or requeue.
    """
    result = queue.enqueue(
        c, priority="high", workflow=workflow, pack_id=pack_id, meta=meta
    )
    job_id = _job_id(queue, c.platform, c.video_id, workflow)
    if job_id is None:
        return None, "enqueue_failed"
    if result == "ignored":
        job = queue.get_job(job_id)
        if job and job["status"] in ("dead", "failed", "cancelled", "partial"):
            # Cancelled/failed rows can still hold a lane while the worker
            # winds down — never requeue or clear cancel under a live slot.
            if _is_active_job(int(job_id)):
                result = "already_processing"
            else:
                # Meta first while still terminal — then open as pending.
                if meta:
                    queue.set_meta(job_id, meta)
                if queue.requeue_job(job_id):
                    _cancel_requested.discard(int(job_id))
                    result = "requeued"
        elif job and job["status"] == "pending":
            if meta:
                queue.set_meta(job_id, meta)
            result = "already_pending"
        elif job and job["status"] in ("done", "published"):
            result = "already_done"
        elif job and job["status"] == "processing":
            result = "already_processing"
    elif result == "skipped_done":
        # enqueue() lumps processing with done — split them so Try does not
        # treat an in-flight job as finished (noop / frames refresh).
        job = queue.get_job(job_id)
        if job and job["status"] == "processing":
            result = "already_processing"
        else:
            result = "already_done"
    if force_requeue and result in ("already_done", "already_pending"):
        if meta:
            queue.set_meta(job_id, meta)
        if queue.requeue_job(job_id, allow_done=True):
            _cancel_requested.discard(int(job_id))
            result = "requeued"
    elif result == "inserted" and meta:
        # INSERT already stored meta; merge again only if caller patches later.
        pass
    return job_id, result


def _article_for_job(content: ContentDB, platform: str, video_id: str) -> dict | None:
    row = content._conn.execute(
        "SELECT id, slug, topic_id, status FROM articles WHERE platform=? AND video_id=?",
        (platform, video_id),
    ).fetchone()
    if not row:
        return None
    return {
        "article_id": int(row["id"]),
        "slug": row["slug"],
        "topic": row["topic_id"],
        "status": row["status"],
    }


def _source_title(
    platform: str,
    video_id: str,
    fallback: str = "",
    content: ContentDB | None = None,
) -> str:
    """Original source video title, instead of queue labels like try:bilibili:BV…."""
    if not video_id:
        return str(fallback or "").strip()

    def usable(value: object) -> str:
        title = str(value or "").strip()
        if not title or title.startswith("try:") or title.lower().startswith("http"):
            return ""
        return title

    meta_path = _work_dir(platform, video_id) / "media" / "fetch_meta.json"
    if meta_path.is_file():
        try:
            title = usable(json.loads(meta_path.read_text(encoding="utf-8-sig")).get("title"))
            if title:
                return title
        except (TypeError, ValueError, json.JSONDecodeError, OSError):
            pass
    if content is not None:
        try:
            row = content._conn.execute(
                "SELECT title_src FROM articles WHERE platform=? AND video_id=?",
                (platform, video_id),
            ).fetchone()
            title = usable(row["title_src"]) if row else ""
            if title:
                return title
        except Exception:
            pass
    return usable(fallback)


def _jobs_ahead(queue: QueueDB, job_id: int) -> int:
    """How many try jobs are ahead of this one (processing + earlier pending)."""
    row = queue.get_job(job_id)
    if row is None:
        return 0
    created = row["created_at"]
    proc = queue._conn.execute(
        """
        SELECT COUNT(*) FROM jobs
        WHERE status='processing' AND priority='high' AND id != ?
        """,
        (job_id,),
    ).fetchone()[0]
    pend = queue._conn.execute(
        """
        SELECT COUNT(*) FROM jobs
        WHERE status='pending' AND priority='high'
          AND created_at < ? AND id != ?
        """,
        (created, job_id),
    ).fetchone()[0]
    return int(proc or 0) + int(pend or 0)


def _schedule_hint(queue: QueueDB, job) -> dict:
    """Explain why a pending job is not running yet (deps vs lane full)."""
    jid = int(job["id"])
    st = str(job["status"] or "")
    lane = _lane_for_job(job)
    hint: dict = {
        "lane": lane,
        "lane_full": False,
        "blocked_on": None,
        "waiting_for_depends_on": False,
    }
    if st != "pending":
        return hint
    if not _deps_satisfied(queue, job):
        meta = _job_meta_dict(job)
        arts = [str(a) for a in (meta.get("depends_on_artifacts") or []) if a]
        deps = [str(d) for d in (meta.get("depends_on") or []) if d]
        if arts:
            hint["blocked_on"] = arts[0]
        elif deps:
            hint["blocked_on"] = deps[0]
        else:
            hint["blocked_on"] = "depends"
        hint["waiting_for_depends_on"] = True
        hint["depends_on"] = deps
        hint["depends_on_artifacts"] = arts
        return hint
    # Deps OK — check whether this lane has free capacity.
    with _run_lock:
        lane_n = len(_active_jobs.get(lane) or [])
        total = sum(len(v) for v in _active_jobs.values())
    if _max_active_total() <= 1:
        hint["lane_full"] = total >= 1
    else:
        hint["lane_full"] = lane_n >= _lane_cap(lane) or total >= _max_active_total()
    if hint["lane_full"]:
        hint["blocked_on"] = "lane_full"
    return hint


def _queue_ahead(queue: QueueDB, job_id: int, status: str) -> dict | None:
    """When pending behind a busy worker, describe the job currently running."""
    if status != "pending" or not _busy_flag():
        return None
    ahead_id = _primary_active_job_id()
    if ahead_id is None or ahead_id == job_id:
        row = queue._conn.execute(
            """
            SELECT id FROM jobs
            WHERE status='processing' AND priority='high' AND id != ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (job_id,),
        ).fetchone()
        ahead_id = int(row["id"]) if row else None
    if ahead_id is None or ahead_id == job_id:
        return None
    ahead_job = queue.get_job(ahead_id)
    if ahead_job is None:
        return None
    plat = ahead_job["platform"]
    vid = ahead_job["video_id"]
    title = _source_title(plat, vid, str(ahead_job["title"] or ""))
    if not title:
        title = f"{plat}:{vid}" if vid else f"job #{ahead_id}"
    return {
        "job_id": ahead_id,
        "title": title,
        "progress": infer_try_progress(
            plat, vid, "processing", langs=job_langs(ahead_job), job_id=ahead_id
        ),
    }


def job_snapshot(job_id: int) -> dict:
    queue = QueueDB()
    content: ContentDB | None = None
    try:
        content = ContentDB()
        job = queue.get_job(job_id)
        if job is None:
            return {"ok": False, "error": "job not found", "job_id": job_id}
        platform = job["platform"]
        video_id = job["video_id"]
        article = _article_for_job(content, platform, video_id)
        st = job["status"]
        if st == "processing" and not _is_active_job(job_id):
            latest = _latest_mtime(_work_dir(platform, video_id))
            now = time.time()
            if latest is None or (now - latest) >= _STALE_SEC:
                queue.mark_failed(
                    job_id,
                    "任务已中断（后台重启或超时）。请重新提交。",
                )
                st = "failed"
                job = queue.get_job(job_id) or job
        duration_sec = None
        try:
            if job["duration_sec"] is not None:
                duration_sec = float(job["duration_sec"])
        except (TypeError, ValueError, KeyError):
            duration_sec = None
        if duration_sec is None:
            meta_path = _work_dir(platform, video_id) / "media" / "fetch_meta.json"
            if meta_path.is_file():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8-sig"))
                    if meta.get("duration") is not None:
                        duration_sec = float(meta["duration"])
                except (TypeError, ValueError, json.JSONDecodeError, OSError):
                    pass
        title = _source_title(platform, video_id, str(job["title"] or ""), content)
        steps_summary = queue.steps_summary(int(job_id))
        out: dict = {
            "ok": True,
            "job_id": job_id,
            "status": st,
            "platform": platform,
            "video_id": video_id,
            "workflow": _job_workflow(job),
            "pack_id": _job_pack_id(job, platform, video_id),
            "title": title,
            "topic": job["topic_id"],
            "error": job["last_error"],
            "busy": _busy_flag(),
            "duration_sec": duration_sec,
            "created_at": job["created_at"],
            "updated_at": job["updated_at"],
            "frame_opts": job_frame_opts(job),
            "lane": _lane_for_job(job),
            "steps_summary": steps_summary,
            "active_job_ids": _active_by_lane_snapshot(),
        }
        hint = _schedule_hint(queue, job)
        out["schedule"] = hint
        if hint.get("waiting_for_depends_on"):
            out["waiting_for_depends_on"] = True
            out["blocked_on"] = hint.get("blocked_on")
        elif hint.get("lane_full"):
            out["lane_full"] = True
            out["blocked_on"] = "lane_full"
        pack_rows = queue.list_pack_jobs(out["pack_id"])
        derived_urls: dict[str, str] = {}
        if article or st in ("done", "partial", "published"):
            try:
                from discover.content_db import public_slug
                from media_ops import copy_derived_to_site, derived_public_urls

                work_early = _work_dir(platform, video_id)
                slug_early = str(
                    (article or {}).get("slug") or public_slug(video_id, title)
                )
                with _export_lock:
                    copy_derived_to_site(work_early, slug_early, SITE_PUBLIC)
                derived_urls = derived_public_urls(slug_early, SITE_PUBLIC)
            except Exception:
                derived_urls = {}
        if pack_rows:
            pack_task_rows: list[dict] = []
            for r in pack_rows:
                wf = _job_workflow(r)
                r_status = str(r["status"])
                r_meta = _job_meta_dict(r)
                r_active = _is_active_job(int(r["id"]))
                products = workflow_products(_work_dir(platform, video_id), wf)
                download = derived_urls.get(wf)
                pack_task_rows.append(
                    {
                        "job_id": int(r["id"]),
                        "workflow": wf,
                        "lane": r_meta.get("lane")
                        or lane_for_workflow(wf),
                        "status": r_status,
                        "error": r["last_error"],
                        "input_from": r_meta.get("input_from"),
                        "depends_on": r_meta.get("depends_on") or [],
                        "depends_on_artifacts": r_meta.get("depends_on_artifacts")
                        or [],
                        "steps_summary": queue.steps_summary(int(r["id"])),
                        "progress": infer_workflow_progress(
                            wf,
                            r_status,
                            _work_dir(platform, video_id),
                            active=bool(r_active),
                        ),
                        "products": [
                            {
                                "name": p["name"],
                                "kind": p["kind"],
                                "rel": p["rel"],
                                "bytes": p["bytes"],
                                "url": (
                                    download
                                    if download and p["kind"] == wf
                                    else _file_api_url(int(r["id"]), p["rel"])
                                ),
                            }
                            for p in products
                        ],
                        "download_url": download,
                        "pack_zip_url": f"/api/try/{int(r['id'])}/pack.zip",
                    }
                )
            out["pack_tasks"] = pack_task_rows
            out["pack_zip_url"] = f"/api/try/{int(job_id)}/pack.zip"
            try:
                write_pack_manifest(
                    _work_dir(platform, video_id),
                    pack_id=out["pack_id"],
                    tasks=pack_task_rows,
                )
            except Exception as e:
                print(f"[pack_manifest] skip ({type(e).__name__}: {e})")
        out["pack_zip_url"] = f"/api/try/{int(job_id)}/pack.zip"
        work = _work_dir(platform, video_id)
        try:
            out["artifacts"] = list_pack_artifacts(work)
        except Exception:
            out["artifacts"] = []
        meta = _job_meta_dict(job)
        if meta.get("input_from") is not None:
            out["input_from"] = meta.get("input_from")
        if meta.get("depends_on"):
            out["depends_on"] = meta.get("depends_on")
        if article:
            out["article"] = article
            out["path"] = f"/topics/{article['topic']}/{article['slug']}"
        if st in ("done", "published"):
            from discover.content_db import public_slug
            from media_ops import copy_derived_to_site, derived_public_urls

            work = _work_dir(platform, video_id)
            slug = str((article or {}).get("slug") or public_slug(video_id, title))
            with _export_lock:
                copy_derived_to_site(work, slug, SITE_PUBLIC)
            derived = derived_public_urls(slug, SITE_PUBLIC)
            if derived:
                out["derived"] = derived
        # Prefer pack-scoped progress when this job is a single WF.
        wf_now = _job_workflow(job)
        if wf_now and wf_now != "all" and st in (
            "pending",
            "processing",
            "done",
            "published",
            "failed",
            "dead",
            "cancelled",
        ):
            out["progress"] = _with_eta(
                infer_workflow_progress(
                    wf_now,
                    st,
                    _work_dir(platform, video_id),
                    active=_is_active_job(int(job_id)),
                ),
                job["created_at"],
            )
        elif st in ("pending", "processing", "done", "published", "failed", "dead", "cancelled"):
            out["progress"] = _with_eta(
                infer_try_progress(
                    platform, video_id, st, langs=job_langs(job), job_id=job_id
                ),
                job["created_at"],
            )
        out.update(try_status())
        if st == "pending":
            ahead = _jobs_ahead(queue, job_id)
            if ahead:
                out["queue_position"] = ahead + 1
            qa = _queue_ahead(queue, job_id, st)
            if qa:
                out["queue_ahead"] = qa
        return out
    finally:
        queue.close()
        if content is not None:
            content.close()


def probe_url_duration(url: str) -> dict:
    """Probe URL: duration + whether cookies are required/usable for try form."""
    from fetch_media import resolve_cookies_file

    url = (url or "").strip()
    if len(url) < 8:
        return {"ok": False, "error": "url too short"}
    try:
        platform, video_id = parse_url(url)
    except ValueError as e:
        return {"ok": False, "error": str(e)}

    cookies_required = bool((COOKIE_POLICY.get(platform) or {}).get("required"))
    cookies_present = resolve_cookies_file() is not None
    base: dict = {
        "platform": platform,
        "video_id": video_id,
        "cookies_required": cookies_required,
        "cookies_present": cookies_present,
        "cookies_ok": (not cookies_required) or cookies_present,
    }

    # Reuse known job / fetch meta first (fast).
    queue = QueueDB()
    try:
        job_id = _job_id(queue, platform, video_id)
        if job_id is not None:
            job = queue.get_job(job_id)
            snap = job_snapshot(job_id)
            job_info = {
                "job_id": job_id,
                "job_status": snap.get("status"),
                "langs": job_langs(job) if job else DEFAULT_PACK,
                "frame_opts": job_frame_opts(job) if job else normalize_frame_opts(),
                "path": snap.get("path"),
                "cached": snap.get("status") in ("done", "published"),
            }
            dur = snap.get("duration_sec")
            if dur is not None and float(dur) > 0:
                return {
                    **base,
                    **job_info,
                    "ok": True,
                    "duration_sec": float(dur),
                    "source": "job",
                    "cookies_ok": True if not cookies_required else cookies_present,
                }
            # Still surface cache info when duration missing.
            base = {**base, **job_info}
    finally:
        queue.close()

    meta_path = _work_dir(platform, video_id) / "media" / "fetch_meta.json"
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8-sig"))
            if meta.get("duration") is not None and float(meta["duration"]) > 0:
                return {
                    **base,
                    "ok": True,
                    "duration_sec": float(meta["duration"]),
                    "source": "fetch_meta",
                    "cookies_ok": True if not cookies_required else cookies_present,
                }
        except (TypeError, ValueError, json.JSONDecodeError, OSError):
            pass

    try:
        import yt_dlp

        if platform == "hls":
            from fetch_media import probe_hls_duration

            cf = resolve_cookies_file()
            dur = probe_hls_duration(url, cookies_file=cf)
            if dur is None or float(dur) <= 0:
                return {
                    **base,
                    "ok": False,
                    "error": "duration unavailable (check m3u8 URL / auth_key expiry)",
                    "cookies_ok": True,
                }
            return {
                **base,
                "ok": True,
                "duration_sec": float(dur),
                "source": "ffmpeg-hls",
                "cookies_ok": True,
            }

        if platform == "bilibili":
            from fetch_media import probe_bilibili_duration

            try:
                info = probe_bilibili_duration(url)
                return {
                    **base,
                    "ok": True,
                    "duration_sec": float(info["duration"]),
                    "source": "bilibili-api",
                    "title": info.get("title"),
                    "cookies_ok": True if not cookies_required else cookies_present,
                }
            except Exception as e:
                print(f"[try] bili duration api fail, yt-dlp next ({e})")

        opts: dict = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
        }
        cf = resolve_cookies_file()
        if cf is not None:
            opts["cookiefile"] = str(cf)
        probe_url = url if platform == "hls" else canonical_url(platform, video_id, original_url=url)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(probe_url, download=False)
        dur = info.get("duration") if isinstance(info, dict) else None
        if dur is None or float(dur) <= 0:
            return {
                **base,
                "ok": False,
                "error": "duration unavailable",
                "cookies_ok": False if cookies_required else True,
            }
        return {
            **base,
            "ok": True,
            "duration_sec": float(dur),
            "source": "yt_dlp",
            "title": (info.get("title") if isinstance(info, dict) else None),
            "cookies_ok": True,
        }
    except Exception as e:
        err = str(e)[:300]
        cookie_fail = cookies_required and (
            "Fresh cookies" in err
            or "cookie" in err.lower()
            or "cookies" in err.lower()
            or not cookies_present
        )
        return {
            **base,
            "ok": False,
            "error": err,
            "cookies_ok": False if cookie_fail else base["cookies_ok"],
            "need_sessionid": bool(cookie_fail),
        }


def probe_urls(urls: list[str], *, pasted_cookies: str | None = None) -> dict:
    """Aggregate URL parse + per-platform cookie requirements for the try form."""
    from fetch_media import resolve_cookies_file

    valid: list[str] = []
    invalid: list[dict] = []
    counts: dict[str, int] = {}
    seen: set[str] = set()

    for raw in urls:
        url = (raw or "").strip()
        if len(url) < 8:
            continue
        key = url.lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            platform, _vid = parse_url(url)
        except ValueError as e:
            invalid.append({"url": url, "error": str(e)})
            continue
        valid.append(url)
        counts[platform] = counts.get(platform, 0) + 1

    platforms: dict[str, dict] = {}
    block_submit = False
    missing_labels: list[str] = []
    for platform, count in sorted(counts.items()):
        pol = COOKIE_POLICY.get(platform) or {
            "required": False,
            "label": platform,
        }
        required = bool(pol.get("required"))
        soft = bool(pol.get("soft"))
        ok = cookies_satisfied(platform, pasted=pasted_cookies)
        present = ok if required else (_cookies_file_covers_platform(platform) or bool((pasted_cookies or "").strip()))
        entry = {
            "count": count,
            "required": required,
            "soft": soft,
            "present": present,
            "ok": ok,
            "label": pol.get("label") or platform,
        }
        platforms[platform] = entry
        if required and not ok:
            block_submit = True
            missing_labels.append(str(entry["label"]))

    message = ""
    if block_submit:
        message = "Login cookies required for: " + ", ".join(missing_labels)
    elif invalid and not valid:
        message = "No valid links found"

    out: dict = {
        "ok": bool(valid),
        "total_lines": len([u for u in urls if (u or "").strip()]),
        "valid_count": len(valid),
        "invalid_count": len(invalid),
        "valid_urls": valid,
        "invalid": invalid,
        "counts": counts,
        "platforms": platforms,
        "block_submit": block_submit,
        "message": message,
        "cookies_local": resolve_cookies_file() is not None,
    }
    if valid:
        first = probe_url_duration(valid[0])
        if first.get("duration_sec"):
            out["duration_sec"] = first["duration_sec"]
        if first.get("cached"):
            out["cached"] = first.get("cached")
            out["job_status"] = first.get("job_status")
            out["langs"] = first.get("langs")
            out["frame_opts"] = first.get("frame_opts")
    return out


def submit_urls(
    urls: list[str],
    *,
    entries: list[dict] | None = None,
    topic: str = "general",
    frames: str = "auto",
    gif_sec: float = 4.0,
    clips: list | None = None,
    gif_ranges: list | None = None,
    sessionid: str | None = None,
    langs: str | list | None = None,
    want_translate: bool = True,
    want_notes: bool = True,
    want_clips: bool = False,
    stages: str | None = None,
    want_dehardsub: bool = False,
    want_deblur: bool = False,
    want_enhance: bool = False,
    want_compress: bool = False,
    want_concat: bool = False,
    want_remix: bool = False,
    want_publish: bool = False,
    media_opts: dict | None = None,
) -> dict:
    probe = probe_urls(urls, pasted_cookies=sessionid)
    if probe.get("block_submit"):
        return {"ok": False, "error": probe.get("message") or "cookies required"}
    valid = probe.get("valid_urls") or []
    if not valid:
        return {"ok": False, "error": probe.get("message") or "no valid urls"}

    entry_by_url: dict[str, dict] = {}
    for raw in entries or []:
        if not isinstance(raw, dict):
            continue
        url = (raw.get("url") or "").strip()
        if url:
            entry_by_url[url.lower()] = raw

    apply_batch_cookies(sessionid, valid)
    jobs: list[dict] = []
    for url in valid:
        try:
            platform, video_id = parse_url(url)
        except ValueError:
            platform, video_id = "", ""
        dur: float | None = None
        if platform and video_id:
            meta_path = _work_dir(platform, video_id) / "media" / "fetch_meta.json"
            if meta_path.is_file():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8-sig"))
                    if meta.get("duration") is not None:
                        dur = float(meta["duration"])
                except (TypeError, ValueError, json.JSONDecodeError, OSError):
                    dur = None
        entry = entry_by_url.get(url.lower())
        frame_opts = entry_frame_opts(
            global_frames=frames,
            global_gif_sec=gif_sec,
            global_clips=clips,
            global_gif_ranges=gif_ranges,
            entry=entry,
            duration_sec=dur,
        )
        out = submit_url(
            url,
            topic=topic,
            frames=frame_opts["frames"],
            gif_sec=frame_opts["gif_sec"],
            clips=frame_opts.get("clips"),
            gif_ranges=frame_opts.get("gif_ranges"),
            sessionid=None,
            langs=langs,
            want_translate=want_translate,
            want_notes=want_notes,
            want_clips=want_clips,
            stages=stages,
            want_dehardsub=want_dehardsub,
            want_deblur=want_deblur,
            want_enhance=want_enhance,
            want_compress=want_compress,
            want_concat=want_concat,
            want_remix=want_remix,
            want_publish=want_publish,
            media_opts=media_opts,
        )
        jobs.append({"url": url, **out})

    starter: int | None = None
    for item in jobs:
        if not item.get("ok"):
            continue
        enq = item.get("enqueue")
        if enq in ("inserted", "requeued", "already_pending"):
            starter = int(item["job_id"])
            break

    return {
        "ok": True,
        "jobs": jobs,
        "queued": sum(1 for j in jobs if j.get("ok")),
        "failed": sum(1 for j in jobs if not j.get("ok")),
        "started_job_id": starter,
        "probe": probe,
    }


def submit_url(
    url: str,
    *,
    topic: str = "general",
    frames: str = "auto",
    gif_sec: float = 4.0,
    clips: list | None = None,
    gif_ranges: list | None = None,
    sessionid: str | None = None,
    langs: str | list | None = None,
    want_translate: bool = True,
    want_notes: bool = True,
    want_clips: bool = False,
    stages: str | None = None,
    want_dehardsub: bool = False,
    want_deblur: bool = False,
    want_enhance: bool = False,
    want_compress: bool = False,
    want_concat: bool = False,
    want_remix: bool = False,
    want_publish: bool = False,
    media_opts: dict | None = None,
) -> dict:
    url = url.strip()
    if len(url) < 8:
        return {"ok": False, "error": "url too short"}
    try:
        platform, video_id = parse_url(url)
    except ValueError as e:
        return {"ok": False, "error": str(e)}

    sid = (sessionid or "").strip()
    if sid:
        try:
            from fetch_media import upsert_try_cookies

            upsert_try_cookies(sid, platform=platform, url=url)
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    dur: float | None = None
    source_title = ""
    meta_path = _work_dir(platform, video_id) / "media" / "fetch_meta.json"
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8-sig"))
            if meta.get("duration") is not None:
                dur = float(meta["duration"])
            source_title = str(meta.get("title") or "").strip()
        except (TypeError, ValueError, json.JSONDecodeError, OSError):
            dur = None
    # 取消/失败的任务往往在下载前就终止，fetch_meta 里没有标题；提交时补齐，
    # 这样即使后续任务没跑完，历史列表也能显示原始视频标题（B 站/yt-dlp 均支持）。
    if not source_title:
        try:
            source_title = str(probe_url_duration(url).get("title") or "").strip()
        except Exception:
            source_title = ""

    frame_opts = normalize_frame_opts(
        frames, gif_sec, clips, gif_ranges=gif_ranges, duration_sec=dur
    )
    langs_pack = normalize_try_langs(langs)
    c = Candidate(
        platform=platform,
        video_id=video_id,
        url=canonical_url(platform, video_id, original_url=url),
        original_url=url,
        title=source_title or f"try:{platform}:{video_id}",
        topic_id=topic,
        score=9999.0,
        duration_sec=int(dur) if dur is not None else None,
    )
    mode = str(frame_opts.get("frames") or "auto")
    has_clips = bool(frame_opts.get("clips"))
    has_gif = bool(frame_opts.get("gif_ranges"))
    clips_flag = (bool(want_clips) and has_clips) or bool(want_concat)
    want_frames = mode != "none" and (
        has_gif or (mode in ("auto", "gif", "jpg") and want_notes)
    )
    work = _work_dir(platform, video_id)
    has_srt = _pack_has_source_srt(platform, video_id)
    opts_map = media_opts if isinstance(media_opts, dict) else {}
    raw_if = opts_map.get("input_from")
    if not isinstance(raw_if, dict):
        raw_if = opts_map.get("input_from_map")
    input_from_map = _normalize_input_from_map(
        raw_if if isinstance(raw_if, dict) else None
    )
    tasks_spec = expand_workflow_tasks(
        want_translate=want_translate,
        want_notes=want_notes,
        want_clips=clips_flag,
        want_frames=want_frames,
        want_dehardsub=want_dehardsub,
        want_deblur=want_deblur,
        want_enhance=want_enhance,
        want_compress=want_compress,
        want_concat=want_concat,
        want_remix=want_remix,
        want_publish=want_publish,
        stages=stages,
        has_source_srt=has_srt,
        input_from=input_from_map,
    )
    pack = pack_id_for(platform, video_id)
    queue = QueueDB()
    try:
        task_results: list[dict] = []
        starter: int | None = None
        prev_langs = DEFAULT_PACK
        prev_frame_opts = normalize_frame_opts()
        job_status = None
        for spec in tasks_spec:
            wf_name = spec["workflow"]
            force = False
            existing = queue.get_job_by_workflow(platform, video_id, wf_name)
            if existing and existing["status"] in ("done", "published"):
                # Re-run when user explicitly toggled this module again.
                force = True
            meta_patch: dict = {
                "frame_opts": frame_opts,
                "langs": langs_pack,
                "want_translate": bool(want_translate),
                "want_notes": bool(want_notes),
                "workflow": wf_name,
                "pack_id": pack,
                "stages": spec["stages"],
            }
            if spec.get("input_from") is not None:
                meta_patch["input_from"] = spec["input_from"]
            meta_patch["depends_on"] = list(spec.get("depends_on") or [])
            meta_patch["depends_on_artifacts"] = list(
                spec.get("depends_on_artifacts") or []
            )
            meta_patch["lane"] = spec.get("lane") or lane_for_workflow(wf_name)
            meta_patch.update(
                _media_workflow_fields(
                    want_translate=want_translate,
                    want_notes=want_notes,
                    has_media=clips_flag or want_frames,
                    stages=spec["stages"],
                    want_dehardsub=want_dehardsub,
                    want_deblur=want_deblur,
                    want_enhance=want_enhance,
                    want_compress=want_compress,
                    want_concat=want_concat,
                    want_remix=want_remix,
                    want_publish=want_publish,
                    media_opts=media_opts,
                )
            )
            # Per-task stages / handoff win over combined compose.
            meta_patch["stages"] = spec["stages"]
            if spec.get("input_from") is not None:
                meta_patch["input_from"] = spec["input_from"]
            meta_patch["depends_on"] = list(spec.get("depends_on") or [])
            meta_patch["depends_on_artifacts"] = list(
                spec.get("depends_on_artifacts") or []
            )
            meta_patch["lane"] = spec.get("lane") or lane_for_workflow(wf_name)
            jid, result = _upsert_workflow_job(
                queue,
                c,
                workflow=wf_name,
                pack_id=pack,
                force_requeue=force,
                meta=meta_patch,
            )
            if jid is None:
                return {"ok": False, "error": f"enqueue failed for {wf_name}"}
            job = queue.get_job(jid)
            if job:
                prev_langs = job_langs(job)
                prev_frame_opts = job_frame_opts(job)
                job_status = str(job["status"])
            if result == "already_processing":
                # Do not mutate meta under a live worker.
                task_results.append(
                    {
                        "job_id": jid,
                        "workflow": wf_name,
                        "stages": spec["stages"],
                        "enqueue": result,
                        "input_from": spec.get("input_from"),
                        "depends_on": list(spec.get("depends_on") or []),
                        "depends_on_artifacts": list(
                            spec.get("depends_on_artifacts") or []
                        ),
                        "lane": spec.get("lane") or lane_for_workflow(wf_name),
                    }
                )
                # Prefer a newly inserted sibling as starter — never sticky-409
                # the whole pack just because translate is already running.
                continue
            # Refresh meta after upsert (covers already_pending / force paths).
            queue.set_meta(jid, meta_patch)
            task_results.append(
                {
                    "job_id": jid,
                    "workflow": wf_name,
                    "stages": spec["stages"],
                    "enqueue": result,
                    "input_from": spec.get("input_from"),
                    "depends_on": list(spec.get("depends_on") or []),
                    "depends_on_artifacts": list(spec.get("depends_on_artifacts") or []),
                    "lane": meta_patch["lane"],
                }
            )
            if starter is None and result in (
                "inserted",
                "requeued",
                "already_pending",
            ):
                starter = jid
        runnable = next(
            (
                t
                for t in task_results
                if t.get("enqueue") in ("inserted", "requeued", "already_pending")
            ),
            None,
        )
        if runnable is not None:
            starter = int(runnable["job_id"])
            enqueue_status = str(runnable.get("enqueue") or "inserted")
        else:
            if starter is None and task_results:
                starter = int(task_results[0]["job_id"])
            enqueue_status = (
                "already_processing"
                if task_results
                and all(t.get("enqueue") == "already_processing" for t in task_results)
                else (task_results[0].get("enqueue") if task_results else "inserted")
            )
        return {
            "ok": True,
            "job_id": starter,
            "enqueue": enqueue_status,
            "platform": platform,
            "video_id": video_id,
            "pack_id": pack,
            "tasks": task_results,
            "frame_opts": frame_opts,
            "langs": langs_pack,
            "prev_langs": prev_langs,
            "prev_frame_opts": prev_frame_opts,
            "job_status": job_status,
        }
    finally:
        queue.close()


def _prepare_upload(file_bytes: bytes, filename: str) -> tuple[Path, str]:
    uid = uuid.uuid4().hex[:12]
    work_dir = WORK_ROOT / f"upload_{uid}"
    media = job_media_dir(work_dir)
    ext = Path(filename or "upload.mp4").suffix.lower()
    if ext not in {".mp4", ".mkv", ".webm", ".mov", ".m4a", ".wav"}:
        ext = ".mp4"
    src = media / f"source{ext}"
    src.write_bytes(file_bytes)
    wav = media / "full_16k.wav"
    ffmpeg = find_ffmpeg()
    import subprocess

    from media_ops import has_audio_stream

    # Only extract audio when the source actually carries an audio track.
    # Silent/clip videos have no audio stream, and forcing extraction makes
    # the whole upload crash. ASR downstream tolerates a missing wav.
    if has_audio_stream(src):
        try:
            from audio_prep import whisper_wav_cmd

            subprocess.run(
                whisper_wav_cmd(ffmpeg, src, wav),
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError:
            # Audio extraction failed despite a reported stream: keep going
            # without a wav rather than failing the whole upload.
            if wav.exists():
                wav.unlink(missing_ok=True)
    # Keep video for GIF when input is video
    if ext in {".mp4", ".mkv", ".webm", ".mov"} and src.name != "source.mp4":
        target = media / "source.mp4"
        if ext != ".mp4":
            subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-i",
                    str(src),
                    "-c",
                    "copy",
                    str(target),
                ],
                check=True,
                capture_output=True,
            )
        else:
            shutil.copy2(src, target)
    return work_dir, uid


def submit_upload(
    file_bytes: bytes,
    filename: str,
    *,
    topic: str = "general",
    frames: str = "auto",
    gif_sec: float = 4.0,
    clips: list | None = None,
    gif_ranges: list | None = None,
    langs: str | list | None = None,
    frame_opts: dict | None = None,
    want_translate: bool = True,
    want_notes: bool = True,
    want_clips: bool = False,
    stages: str | None = None,
    want_dehardsub: bool = False,
    want_deblur: bool = False,
    want_enhance: bool = False,
    want_compress: bool = False,
    want_concat: bool = False,
    want_remix: bool = False,
    want_publish: bool = False,
    media_opts: dict | None = None,
) -> dict:
    if len(file_bytes) < 10_000:
        return {"ok": False, "error": "file too small"}
    if len(file_bytes) > 500 * 1024 * 1024:
        return {"ok": False, "error": "file too large (max 500MB)"}
    if frame_opts is None:
        frame_opts = normalize_frame_opts(frames, gif_sec, clips, gif_ranges=gif_ranges)
    langs_pack = normalize_try_langs(langs)
    try:
        work_dir, uid = _prepare_upload(file_bytes, filename)
    except Exception as e:
        return {"ok": False, "error": f"upload prepare failed: {e}"}
    wav = existing_wav(work_dir)
    c = Candidate(
        platform="upload",
        video_id=uid,
        url=f"upload://{uid}",
        original_url=f"upload://{uid}",
        title=Path(filename or "upload").stem[:80] or f"upload:{uid}",
        topic_id=topic,
        score=9999.0,
    )
    mode = str(frame_opts.get("frames") or "auto")
    has_clips = bool(frame_opts.get("clips"))
    has_gif = bool(frame_opts.get("gif_ranges"))
    clips_flag = (bool(want_clips) and has_clips) or bool(want_concat)
    want_frames = mode != "none" and (
        has_gif or (mode in ("auto", "gif", "jpg") and want_notes)
    )
    opts_map = media_opts if isinstance(media_opts, dict) else {}
    raw_if = opts_map.get("input_from")
    if not isinstance(raw_if, dict):
        raw_if = opts_map.get("input_from_map")
    input_from_map = _normalize_input_from_map(
        raw_if if isinstance(raw_if, dict) else None
    )
    tasks_spec = expand_workflow_tasks(
        want_translate=want_translate,
        want_notes=want_notes,
        want_clips=clips_flag,
        want_frames=want_frames,
        want_dehardsub=want_dehardsub,
        want_deblur=want_deblur,
        want_enhance=want_enhance,
        want_compress=want_compress,
        want_concat=want_concat,
        want_remix=want_remix,
        want_publish=want_publish,
        stages=stages,
        has_source_srt=False,
        input_from=input_from_map,
    )
    pack = pack_id_for("upload", uid)
    queue = QueueDB()
    try:
        task_results: list[dict] = []
        starter: int | None = None
        for spec in tasks_spec:
            meta_patch = {
                "frame_opts": frame_opts,
                "langs": langs_pack,
                "want_translate": bool(want_translate),
                "want_notes": bool(want_notes),
                "workflow": spec["workflow"],
                "pack_id": pack,
                "stages": spec["stages"],
            }
            if spec.get("input_from") is not None:
                meta_patch["input_from"] = spec["input_from"]
            meta_patch["depends_on"] = list(spec.get("depends_on") or [])
            meta_patch["depends_on_artifacts"] = list(
                spec.get("depends_on_artifacts") or []
            )
            meta_patch["lane"] = spec.get("lane") or lane_for_workflow(spec["workflow"])
            meta_patch.update(
                _media_workflow_fields(
                    want_translate=want_translate,
                    want_notes=want_notes,
                    has_media=clips_flag or want_frames,
                    stages=spec["stages"],
                    want_dehardsub=want_dehardsub,
                    want_deblur=want_deblur,
                    want_enhance=want_enhance,
                    want_compress=want_compress,
                    want_concat=want_concat,
                    want_remix=want_remix,
                    want_publish=want_publish,
                    media_opts=media_opts,
                )
            )
            meta_patch["stages"] = spec["stages"]
            if spec.get("input_from") is not None:
                meta_patch["input_from"] = spec["input_from"]
            meta_patch["depends_on"] = list(spec.get("depends_on") or [])
            meta_patch["depends_on_artifacts"] = list(
                spec.get("depends_on_artifacts") or []
            )
            meta_patch["lane"] = spec.get("lane") or lane_for_workflow(spec["workflow"])
            jid, result = _upsert_workflow_job(
                queue,
                c,
                workflow=spec["workflow"],
                pack_id=pack,
                meta=meta_patch,
            )
            if jid is None:
                return {"ok": False, "error": f"enqueue failed for {spec['workflow']}"}
            if wav:
                queue.set_source_wav(jid, str(wav))
            queue.set_meta(jid, meta_patch)
            task_results.append(
                {
                    "job_id": jid,
                    "workflow": spec["workflow"],
                    "stages": spec["stages"],
                    "enqueue": result,
                    "input_from": spec.get("input_from"),
                    "depends_on": list(spec.get("depends_on") or []),
                    "depends_on_artifacts": list(spec.get("depends_on_artifacts") or []),
                    "lane": meta_patch["lane"],
                }
            )
            if starter is None and result in (
                "inserted",
                "requeued",
                "already_pending",
            ):
                starter = jid
        if starter is None and task_results:
            starter = int(task_results[0]["job_id"])
        return {
            "ok": True,
            "job_id": starter,
            "enqueue": "inserted",
            "platform": "upload",
            "video_id": uid,
            "pack_id": pack,
            "tasks": task_results,
            "work_dir": str(work_dir),
            "frame_opts": frame_opts,
            "langs": langs_pack,
        }
    finally:
        queue.close()


def submit_uploads(
    items: list[dict],
    *,
    topic: str = "general",
    frames: str = "auto",
    gif_sec: float = 4.0,
    clips: list | None = None,
    gif_ranges: list | None = None,
    langs: str | list | None = None,
    want_translate: bool = True,
    want_notes: bool = True,
    want_clips: bool = False,
    stages: str | None = None,
    want_dehardsub: bool = False,
    want_deblur: bool = False,
    want_enhance: bool = False,
    want_compress: bool = False,
    want_concat: bool = False,
    want_remix: bool = False,
    want_publish: bool = False,
    media_opts: dict | None = None,
) -> dict:
    """Batch upload enqueue; chains like submit_urls."""
    if not items:
        return {"ok": False, "error": "no files"}

    jobs: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        raw = item.get("bytes")
        name = str(item.get("filename") or "upload.mp4")
        if not isinstance(raw, (bytes, bytearray)):
            jobs.append({"filename": name, "ok": False, "error": "missing bytes"})
            continue
        entry = item.get("entry") if isinstance(item.get("entry"), dict) else None
        dur: float | None = None
        try:
            if item.get("duration_sec") is not None:
                dur = float(item["duration_sec"])
        except (TypeError, ValueError):
            dur = None
        fo = entry_frame_opts(
            global_frames=frames,
            global_gif_sec=gif_sec,
            global_clips=clips,
            global_gif_ranges=gif_ranges,
            entry=entry,
            duration_sec=dur,
        )
        out = submit_upload(
            bytes(raw),
            name,
            topic=topic,
            frame_opts=fo,
            langs=langs,
            want_translate=want_translate,
            want_notes=want_notes,
            want_clips=want_clips,
            stages=stages,
            want_dehardsub=want_dehardsub,
            want_deblur=want_deblur,
            want_enhance=want_enhance,
            want_compress=want_compress,
            want_concat=want_concat,
            want_remix=want_remix,
            want_publish=want_publish,
            media_opts=media_opts,
        )
        jobs.append({"filename": name, **out})

    starter: int | None = None
    for item in jobs:
        if not item.get("ok"):
            continue
        enq = item.get("enqueue")
        if enq in ("inserted", "requeued", "already_pending"):
            starter = int(item["job_id"])
            break

    return {
        "ok": True,
        "jobs": jobs,
        "queued": sum(1 for j in jobs if j.get("ok")),
        "failed": sum(1 for j in jobs if not j.get("ok")),
        "started_job_id": starter,
    }


def refresh_try_frames(job_id: int, *, stages: str | None = None) -> dict:
    """Re-cut GIF/MP4 on an already-done job (no ASR). Delegates to batch media-only."""
    prev_status = "done"
    queue_probe = QueueDB()
    try:
        job_probe = queue_probe.get_job(int(job_id))
        if job_probe is None:
            return {"ok": False, "error": "job not found"}
        st = str(job_probe["status"] or "")
        prev_status = st
        if st not in ("done", "published", "partial"):
            return {
                "ok": False,
                "error": f"refresh only for finished jobs (got {st})",
            }
        lane = _lane_for_job(job_probe)
    finally:
        queue_probe.close()
    if not _try_acquire_slot(lane, job_id):
        return {"ok": False, "error": "lane slot busy — retry later"}
    try:
        from discover.run_batch import (
            JobCancelled,
            _process_media_only,
            resolve_media_refresh_stages,
        )

        queue = QueueDB()
        content: ContentDB | None = None
        try:
            content = ContentDB()
            job = queue.get_job(job_id)
            if job is None:
                return {"ok": False, "error": "job not found"}
            platform = job["platform"]
            video_id = job["video_id"]
            work_dir = _work_dir(platform, video_id)
            opts = job_frame_opts(job)
            enabled = resolve_media_refresh_stages(
                stages=stages,
                frames=opts.get("frames"),
                has_clips=bool(opts.get("clips")),
            )

            queue._conn.execute(
                """
                UPDATE jobs SET status='processing', updated_at=?
                WHERE id=? AND status IN ('done', 'published', 'partial')
                """,
                (time.time(), job_id),
            )
            queue._conn.commit()

            if "frames" in enabled:
                clear_keypoint_images(work_dir)

            try:
                _process_media_only(
                    job,
                    queue=queue,
                    content=content,
                    work_dir=work_dir,
                    enabled=enabled,
                    frame_mode=opts.get("frames"),
                    gif_duration=opts.get("gif_sec"),
                    frame_clips=list(opts.get("clips") or []),
                    should_cancel=lambda: int(job_id) in _cancel_requested,
                )
            except JobCancelled:
                _apply_cancel(queue, int(job_id))
                return job_snapshot(job_id)
            if int(job_id) in _cancel_requested:
                _apply_cancel(queue, int(job_id))
                return job_snapshot(job_id)
            with _export_lock:
                export_main([])
            snap = job_snapshot(job_id)
            snap["stages"] = sorted(enabled)
            snap["clips_only"] = enabled == frozenset({"clips"})
            return snap
        except Exception as e:
            # Never demote a finished job to failed on a refresh glitch.
            # Never revive a cancelled row back to published/done.
            try:
                cur = queue.get_job(int(job_id))
                if cur and str(cur["status"] or "") == "cancelled":
                    return {"ok": False, "error": "cancelled", "job_id": job_id}
                if int(job_id) in _cancel_requested:
                    _apply_cancel(queue, int(job_id))
                    return {"ok": False, "error": "cancelled", "job_id": job_id}
                if prev_status == "partial":
                    queue.mark_partial(job_id, error=str(e)[:2000])
                elif prev_status == "published":
                    queue._conn.execute(
                        """
                        UPDATE jobs SET status='published', last_error=?, updated_at=?
                        WHERE id=? AND status NOT IN ('cancelled')
                        """,
                        (str(e)[:2000], time.time(), int(job_id)),
                    )
                    queue._conn.commit()
                else:
                    queue.mark_done(job_id)
                    queue._conn.execute(
                        """
                        UPDATE jobs SET last_error=?, updated_at=?
                        WHERE id=? AND status NOT IN ('cancelled')
                        """,
                        (f"refresh failed: {str(e)[:500]}", time.time(), int(job_id)),
                    )
                    queue._conn.commit()
            except Exception:
                pass
            return {"ok": False, "error": str(e)[:2000], "job_id": job_id}
        finally:
            queue.close()
            if content is not None:
                content.close()
    finally:
        _release_slot(lane, job_id)
        schedule_next_try_job(skip_job_id=job_id)


def run_try_job(
    job_id: int,
    *,
    _slot_held: bool = False,
    _slot_lane: str | None = None,
) -> dict:
    """Background: process one queued job, export site."""
    queue_probe = QueueDB()
    try:
        job_probe = queue_probe.get_job(job_id)
        lane = _slot_lane or (
            _lane_for_job(job_probe) if job_probe is not None else LANE_CONTENT
        )
    finally:
        queue_probe.close()

    if not _slot_held:
        if not _try_acquire_slot(lane, job_id):
            return {"ok": False, "error": "another try job is running"}
    try:
        from discover.run_batch import JobCancelled, process_job
        from llm.factory import configure_llm

        configure_llm(profile="local")
        queue = QueueDB()
        content: ContentDB | None = None
        try:
            content = ContentDB()
            job = queue.get_job(job_id)
            if job is None:
                return {"ok": False, "error": "job not found"}
            if job["status"] == "done":
                with _export_lock:
                    export_main([])
                return job_snapshot(job_id)
            if job["status"] in ("dead", "failed"):
                return job_snapshot(job_id)

            if job["status"] == "pending" and not _deps_satisfied(queue, job):
                return {
                    "ok": False,
                    "waiting_for_depends_on": True,
                    "job_id": job_id,
                    "depends_on": list(_job_meta_dict(job).get("depends_on") or []),
                    "depends_on_artifacts": list(
                        _job_meta_dict(job).get("depends_on_artifacts") or []
                    ),
                }

            claimed = queue.claim_next(max_attempts=5, job_id=job_id)
            if claimed is None:
                return job_snapshot(job_id)
            job = claimed
            job_id = int(job["id"])

            platform = job["platform"]
            video_id = job["video_id"]
            url = job["canonical_url"] or job["url"]
            frame_opts = job_frame_opts(job)
            langs_pack = job_langs(job)
            stages = "all"
            want_translate = True
            want_notes = True
            workflow = _job_workflow(job)
            meta_json = _job_field(job, "meta_json")
            if meta_json:
                try:
                    meta = json.loads(meta_json)
                    if isinstance(meta, dict):
                        if meta.get("stages"):
                            stages = str(meta["stages"])
                        if "want_translate" in meta:
                            want_translate = bool(meta["want_translate"])
                        if "want_notes" in meta:
                            want_notes = bool(meta["want_notes"])
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
            # Independent WF jobs: skip flags follow this task's stages, not pack toggles.
            if workflow not in ("all", ""):
                enabled = {p.strip() for p in stages.split(",") if p.strip()}
                want_translate = "translate" in enabled
                want_notes = bool(enabled & {"notes", "localize"})

            if platform in ("bilibili", "youtube", "hls"):
                work_dir = WORK_ROOT / f"{platform}_{video_id}"
                try:
                    if job_id in _cancel_requested:
                        _apply_cancel(queue, job_id)
                        return job_snapshot(job_id)
                    ensure_source_video(url, work_dir, cookies_from_browser=None)
                except Exception as e:
                    print(f"[try] video prefetch skip ({e})")
                # Cancel during long prefetch must not leave a sticky flag.
                if job_id in _cancel_requested:
                    _apply_cancel(queue, job_id)
                    return job_snapshot(job_id)

            try:
                if job_id in _cancel_requested:
                    _apply_cancel(queue, job_id)
                    return job_snapshot(job_id)
                print(
                    f"[try] workflow={workflow} langs={langs_pack} stages={stages} "
                    f"translate={want_translate} notes={want_notes}"
                )
                process_job(
                    job,
                    queue=queue,
                    content=content,
                    work_root=WORK_ROOT,
                    langs=langs_pack,
                    source_lang="auto",
                    chat_model="gemma4:e2b",
                    translate_model="translategemma:4b",
                    # Publish-quality ASR: multipass + GPU when available (RTX 2060 → int8).
                    device="cuda",
                    multipass=True,
                    llm_correct=True,
                    skip_translate=not want_translate,
                    skip_summary=not want_notes,
                    skip_keypoints=not want_notes,
                    localize_summary=want_notes,
                    max_attempts=5,
                    dry_run=False,
                    skip_fetch=(platform == "upload"),
                    frame_mode=frame_opts["frames"],
                    gif_duration=frame_opts["gif_sec"],
                    frame_clips=frame_opts.get("clips") or None,
                    stages=stages,
                    should_cancel=lambda: job_id in _cancel_requested,
                )
            except JobCancelled:
                _apply_cancel(queue, job_id)
                return job_snapshot(job_id)
            except Exception as e:
                if job_id in _cancel_requested:
                    _apply_cancel(queue, job_id)
                    return job_snapshot(job_id)
                queue.mark_failed(job_id, str(e)[:2000])
                _cascade_fail_dependents(
                    queue,
                    platform,
                    video_id,
                    workflow,
                    f"依赖任务失败：{workflow}（{str(e)[:120]}）",
                )
                raise

            if job_id in _cancel_requested:
                _apply_cancel(queue, job_id)
                return job_snapshot(job_id)

            with _export_lock:
                export_main([])
            return job_snapshot(job_id)
        finally:
            # Always drop sticky cancel so a later requeue of this id is clean.
            _cancel_requested.discard(int(job_id))
            queue.close()
            if content is not None:
                content.close()
    finally:
        _release_slot(lane, job_id)
        schedule_next_try_job(skip_job_id=job_id)
