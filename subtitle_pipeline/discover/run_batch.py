#!/usr/bin/env python3
"""Batch: SQLite pending jobs → fetch → ASR/translate/summary → content.db."""

from __future__ import annotations

import argparse
import json
import re
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from discover.content_db import ContentDB, embed_url_for, public_slug, slugify
from discover.queue_db import QueueDB
from langs import coalesce_source_lang, file_tag, normalize_lang, resolve_targets
from job_layout import existing_wav, is_usable_wav, job_media_dir, list_locale_srts, locale_srt_path
from pipeline import (
    DEFAULT_CHAT,
    DEFAULT_LANG_WORKERS,
    DEFAULT_TRANSLATE,
    LOCAL_WHISPER,
    auto_build_glossary,
    correct_segments,
    do_keypoints,
    do_summary,
    do_translate,
    ensure_ollama_model,
    find_ollama,
    lang_workers,
    locale_summary_json,
    load_notes_summary,
    locked_print,
    notes_fields,
    ollama_chat,
    segments_plain_text,
    transcribe_video,
    write_locale_notes,
    write_notes_index,
    write_srt,
)


def _job_work_dir(base: Path, platform: str, video_id: str) -> Path:
    safe = re.sub(r"[^\w.-]+", "_", f"{platform}_{video_id}")
    d = base / safe
    d.mkdir(parents=True, exist_ok=True)
    return d


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


ALL_STAGES = frozenset(
    {"fetch", "asr", "translate", "notes", "localize", "frames", "clips"}
)
POSTPROC_STAGES = frozenset(
    {"dehardsub", "enhance", "compress", "concat", "remix", "publish"}
)
KNOWN_STAGES = ALL_STAGES | POSTPROC_STAGES
MEDIA_ONLY_STAGES = frozenset({"frames", "clips"}) | POSTPROC_STAGES


def parse_stages(raw: str | None) -> frozenset[str]:
    """Stage set for controllable batch / try refresh.

    Presets:
      all | media | clips | frames
      llm  = translate,notes,localize (reuse existing SRT)
      post = llm + frames,clips
      Or comma list: translate,notes,localize,dehardsub,enhance,compress,concat,remix,publish
    ``all`` does **not** include dehardsub/enhance/compress/concat/remix/publish (opt-in).
    """
    s = (raw or "all").strip().lower()
    if s in ("", "all"):
        return ALL_STAGES
    if s in ("media", "gif+mp4", "gif_mp4"):
        return frozenset({"frames", "clips"})
    if s in ("clips", "mp4", "clip"):
        return frozenset({"clips"})
    if s in ("frames", "gif", "jpg"):
        return frozenset({"frames"})
    if s in ("llm", "text"):
        return frozenset({"translate", "notes", "localize"})
    if s in ("post", "from-srt", "from_srt"):
        return frozenset({"translate", "notes", "localize", "frames", "clips"})
    if s in ("dehardsub", "strip_hardsubs", "hardsub", "unburn"):
        return frozenset({"dehardsub"})
    if s in ("enhance", "compress", "concat", "remix", "publish"):
        return frozenset({s})
    parts = {p.strip() for p in s.replace(";", ",").split(",") if p.strip()}
    # Aliases → canonical stage name
    if "strip_hardsubs" in parts or "hardsub" in parts or "unburn" in parts:
        parts.discard("strip_hardsubs")
        parts.discard("hardsub")
        parts.discard("unburn")
        parts.add("dehardsub")
    if "all" in parts:
        parts.discard("all")
        parts |= set(ALL_STAGES)
    if "media" in parts:
        parts.discard("media")
        parts |= {"frames", "clips"}
    if "llm" in parts:
        parts.discard("llm")
        parts |= {"translate", "notes", "localize"}
    if "post" in parts:
        parts.discard("post")
        parts |= {"translate", "notes", "localize", "frames", "clips"}
    unknown = parts - KNOWN_STAGES
    if unknown:
        raise ValueError(f"unknown stages: {', '.join(sorted(unknown))}")
    if not parts:
        return ALL_STAGES
    return frozenset(parts)


def resolve_media_refresh_stages(
    *,
    stages: str | None,
    frames: str | None,
    has_clips: bool,
) -> frozenset[str]:
    """Media-only stage set for already-done try / --stages clips|media|frames."""
    if stages:
        enabled = parse_stages(stages) & MEDIA_ONLY_STAGES
        return enabled or frozenset({"frames", "clips"})
    mode = (frames or "auto").strip().lower()
    if mode == "none" and has_clips:
        return frozenset({"clips"})
    if not has_clips:
        return frozenset({"frames"})
    return frozenset({"frames", "clips"})


def _detect_source_lang(args_source: str, srt_path: Path | None) -> str:
    if args_source and args_source != "auto":
        return coalesce_source_lang(args_source)
    if srt_path:
        stem = srt_path.stem
        if srt_path.parent.name == "subs":
            return coalesce_source_lang(stem)
        if "_" in stem:
            return coalesce_source_lang(stem.rsplit("_", 1)[-1])
    return "en"


def _find_source_srt(work_dir: Path, stem: str, source_lang: str) -> Path | None:
    p = locale_srt_path(work_dir, source_lang, stem)
    if p.exists():
        return p
    found = list_locale_srts(work_dir, stem)
    tag = file_tag(source_lang)
    return found.get(tag) or next(iter(found.values()), None)


def _load_existing_job_segments(
    work_dir: Path,
    *,
    source_lang: str,
    stem: str = "full_16k",
) -> tuple[list[dict], Path, str, str, Path]:
    """Load source SRT from an existing work dir (skip Whisper)."""
    from pipeline import parse_srt

    out_dir = Path(work_dir)
    src_lang = coalesce_source_lang(source_lang if source_lang != "auto" else "zh")
    src_srt = _find_source_srt(out_dir, stem, src_lang)
    if src_srt is None:
        raise FileNotFoundError(
            f"stages omit asr but no source SRT in {out_dir / 'subs'}"
        )
    src_lang = _detect_source_lang(source_lang, src_srt)
    segs = parse_srt(src_srt)
    print(f"[asr] reuse {len(segs)} cues from {src_srt}")
    return segs, out_dir, stem, src_lang, src_srt


def _load_existing_glossary(out_dir: Path) -> dict:
    gloss_path = job_media_dir(out_dir) / "glossary.json"
    if not gloss_path.is_file():
        return {}
    try:
        data = json.loads(gloss_path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


def _resolve_frame_opts(
    job,
    *,
    frame_mode: str | None,
    gif_duration: float | None,
    frame_clips: list | None,
) -> tuple[str, float | None, list | None, list | None]:
    """Merge explicit args with job meta frame_opts."""
    mode = (frame_mode or "").strip().lower() or None
    clips = list(frame_clips) if frame_clips is not None else None
    gif_ranges = None
    gif_sec = gif_duration
    meta_json = _job_field(job, "meta_json")
    if meta_json:
        try:
            meta = json.loads(meta_json)
            opts = meta.get("frame_opts") if isinstance(meta, dict) else None
            if isinstance(opts, dict):
                if mode is None:
                    mode = str(opts.get("frames") or "auto")
                if gif_sec is None and opts.get("gif_sec") is not None:
                    gif_sec = float(opts["gif_sec"])
                if clips is None and isinstance(opts.get("clips"), list):
                    clips = list(opts["clips"])
                if isinstance(opts.get("gif_ranges"), list):
                    gif_ranges = list(opts["gif_ranges"])
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    if mode not in ("auto", "none", "gif", "jpg"):
        mode = "auto"
    return mode, gif_sec, clips, gif_ranges


def _job_media_opts(job) -> dict:
    from media_ops import normalize_media_opts

    meta_json = _job_field(job, "meta_json")
    if meta_json:
        try:
            meta = json.loads(meta_json)
            if isinstance(meta, dict):
                raw = meta.get("media_opts")
                if isinstance(raw, dict):
                    return normalize_media_opts(raw)
                return normalize_media_opts(meta)
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    return normalize_media_opts()


def _run_job_postproc(
    work_dir: Path,
    job,
    enabled: frozenset[str],
    *,
    frame_clips: list | None = None,
) -> None:
    if not (enabled & POSTPROC_STAGES):
        return
    from media_ops import run_postproc

    _mode, _gif, clips, _gif_ranges = _resolve_frame_opts(
        job, frame_mode=None, gif_duration=None, frame_clips=frame_clips
    )
    produced = run_postproc(
        work_dir,
        enabled,
        media_opts=_job_media_opts(job),
        clip_ranges=clips,
    )
    if "publish" in enabled:
        from publish_ops import run_publish

        opts = _job_media_opts(job)
        plats = opts.get("publish_platforms") or []
        pub = run_publish(
            work_dir,
            platforms=list(plats) if plats else None,
        )
        produced["publish"] = Path(pub["report"])
    print(f"[postproc] { {k: str(v.name) for k, v in produced.items()} }")


def _attach_job_media(
    *,
    work_dir: Path,
    job,
    summary: dict | None,
    src_lang: str,
    enabled: frozenset[str],
    frame_mode: str | None,
    gif_duration: float | None,
    frame_clips: list | None,
) -> dict | None:
    """Attach GIF/JPG and/or MP4 ranges. Shared by full batch and media-only."""
    if "frames" not in enabled and "clips" not in enabled:
        return summary
    if not summary:
        if "clips" in enabled:
            from media_ops import cut_range_clips
            from note_frames import existing_source_video

            mode, _gif_sec, clips, _gif_ranges = _resolve_frame_opts(
                job,
                frame_mode=frame_mode,
                gif_duration=gif_duration,
                frame_clips=frame_clips,
            )
            video = existing_source_video(work_dir)
            if clips and video:
                try:
                    cut_range_clips(work_dir, clips, video=video)
                    print("[clips] cut without notes")
                except Exception as e:
                    print(f"[clips] FAILED without notes ({type(e).__name__}: {e})")
            elif "clips" in enabled:
                print("[clips] skipped — no ranges or source video (and no notes)")
        else:
            print("[frames] skipped — no source notes")
        return summary

    from frame_policy import decide_frames
    from note_frames import (
        attach_gif_ranges,
        attach_keypoint_frames,
        attach_video_range_clips,
        existing_source_video,
    )

    mode, gif_sec, clips, gif_ranges = _resolve_frame_opts(
        job,
        frame_mode=frame_mode,
        gif_duration=gif_duration,
        frame_clips=frame_clips,
    )
    force = None if mode == "auto" else mode
    video = existing_source_video(work_dir)
    video_id = _job_field(job, "video_id")
    topic_id = _job_field(job, "topic_id") or "home_tips"
    slug = public_slug(
        video_id,
        str(summary.get("title") or _job_field(job, "title") or video_id),
    )
    print(
        f"[frames] stages={sorted(enabled & frozenset({'frames', 'clips'}))} "
        f"clips={len(clips or [])} gif_ranges={len(gif_ranges or [])} "
        f"video={'yes' if video else 'no'}"
    )

    if "frames" in enabled and mode != "none" and video:
        if gif_ranges:
            media = force if force in ("gif", "jpg") else "gif"
            print(
                f"[frames] custom gif_ranges={len(gif_ranges)} media={media} "
                "(bypass topic policy)"
            )
            summary = (
                attach_gif_ranges(
                    work_dir,
                    slug=slug,
                    source_lang=src_lang,
                    ranges=gif_ranges,
                    media=media,
                    max_dur=float(gif_sec or 20),
                )
                or summary
            )
        else:
            decision = decide_frames(
                topic_id=topic_id,
                title=str(summary.get("title") or _job_field(job, "title") or ""),
                key_points=list(summary.get("key_points") or []),
                has_video=bool(video),
                force_media=force,
                gif_duration=gif_sec,
            )
            print(f"[frames] decision={decision.as_dict()}")
            if decision.enabled:
                summary = (
                    attach_keypoint_frames(
                        work_dir,
                        slug=slug,
                        source_lang=src_lang,
                        media=decision.media,
                        gif_duration=decision.gif_duration,
                    )
                    or summary
                )

    if "clips" in enabled and video:
        if clips is None:
            print("[clips] skipped — no ranges in frame_opts")
        else:
            summary = (
                attach_video_range_clips(
                    work_dir,
                    slug=slug,
                    source_lang=src_lang,
                    clips=clips,
                )
                or summary
            )
    elif "clips" in enabled and not video:
        print("[clips] skipped — no source video")
    return summary


def _load_summary(path: Path) -> dict:
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8-sig")
    if raw.startswith("\ufeff"):
        raw = raw.lstrip("\ufeff")
    return json.loads(raw)


def translate_summary_locale(
    ollama: str,
    model: str,
    summary: dict,
    *,
    source_lang: str,
    target_lang: str,
) -> dict:
    """Localize title/one_liner/notes lists via translategemma."""
    src = normalize_lang(source_lang)
    tgt = normalize_lang(target_lang)
    payload = notes_fields(summary)
    if src == tgt:
        return payload
    prompt = (
        f"Translate this JSON from {src} into {tgt}. "
        "Keep the same keys and array lengths. "
        "one_liner = short teaser; summary = 总体总结 paragraph (3-5 sentences). "
        "focuses = 重点, key_points = 要点, hard_points = 难点. "
        "Keep digits, ratios, minutes, and product/place names. "
        "Do not turn concrete steps into slogans. "
        "Return ONLY JSON.\n\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )
    from pipeline import extract_json_object

    data: dict = {}
    last_err: Exception | None = None
    for attempt in range(1, 4):
        raw = ollama_chat(ollama, model, prompt, timeout=180, role="translate")
        try:
            data = extract_json_object(raw)
            if data:
                break
            last_err = ValueError("empty JSON from translate")
        except (json.JSONDecodeError, ValueError) as e:
            last_err = e
            locked_print(
                f"[notes-loc] {tgt} JSON retry {attempt}/3 ({type(e).__name__}: {e})"
            )
    else:
        locked_print(
            f"[notes-loc] {tgt} keep source fields after JSON failure "
            f"({type(last_err).__name__}: {last_err})"
        )
        data = {}
    merged = {**payload, **notes_fields(data)}
    if not merged["title"]:
        merged["title"] = payload["title"]
    if not merged["one_liner"]:
        merged["one_liner"] = payload["one_liner"]
    if not merged.get("summary"):
        merged["summary"] = payload.get("summary") or payload["one_liner"]
    if not merged["focuses"]:
        merged["focuses"] = payload["focuses"]
    if not merged["key_points"]:
        merged["key_points"] = payload["key_points"]
    if not merged["hard_points"]:
        merged["hard_points"] = payload["hard_points"]
    # Keep screenshot / timestamp media from the source notes by index
    for key in ("focuses", "key_points", "hard_points"):
        src_list = payload.get(key) or []
        tgt_list = merged.get(key) or []
        if not isinstance(src_list, list) or not isinstance(tgt_list, list):
            continue
        for i, item in enumerate(tgt_list):
            if not isinstance(item, dict) or i >= len(src_list):
                continue
            src_item = src_list[i]
            if not isinstance(src_item, dict):
                continue
            if src_item.get("image") and not item.get("image"):
                item["image"] = src_item["image"]
            if "start_sec" in src_item and "start_sec" not in item:
                item["start_sec"] = src_item["start_sec"]
    return merged


def register_work_dir(
    content: ContentDB,
    *,
    platform: str,
    video_id: str,
    topic_id: str,
    canonical_url: str,
    work_dir: Path,
    source_lang: str,
    title: str | None = None,
    author: str | None = None,
    published_at: float | None = None,
    views: int | None = None,
    duration_sec: int | None = None,
    thumb_url: str | None = None,
    source_wav: str | None = None,
    locales: list[str] | None = None,
) -> int:
    """Scan work_dir for SRT/summary and write content.db rows."""
    srt_map = list_locale_srts(work_dir)
    stem = "full_16k"
    if not srt_map:
        for p in work_dir.glob("*.srt"):
            stem = p.stem.rsplit("_", 1)[0]
            break
        srt_map = list_locale_srts(work_dir, stem)

    summary = load_notes_summary(work_dir, stem, source_lang)
    if not summary:
        summary = _load_summary(work_dir / f"{stem}_summary.json")
    title_src = title or summary.get("title") or f"{platform}:{video_id}"
    src_lang = coalesce_source_lang(summary.get("source_lang") or source_lang)
    # Prefer real notes title for display; keep slug aligned with frame paths.
    display_title = summary.get("title") or title_src
    if str(title_src).startswith("try:") or str(title_src).startswith("upload:"):
        display_title = summary.get("title") or title_src

    article_id = content.upsert_article(
        {
            "platform": platform,
            "video_id": video_id,
            "topic_id": topic_id,
            "slug": public_slug(video_id, display_title),
            "canonical_url": canonical_url,
            "embed_url": embed_url_for(platform, video_id),
            "source_lang": src_lang,
            "title_src": display_title,
            "author": author,
            "published_at": published_at,
            "views": views,
            "duration_sec": duration_sec,
            "thumb_url": thumb_url,
            "source_wav": source_wav or (
                str(existing_wav(work_dir)) if existing_wav(work_dir) else None
            ),
            "work_dir": str(work_dir),
            "status": "ready",
        }
    )

    src_tag = file_tag(src_lang)
    src_srt = _find_source_srt(work_dir, stem, src_lang)

    # source locale
    src_fields = notes_fields(summary)
    content.upsert_locale(
        article_id,
        src_lang,
        {
            **src_fields,
            "srt_path": str(src_srt) if src_srt else None,
            "txt_path": None,
            "summary_json_path": str(locale_summary_json(work_dir, stem, src_lang))
            if locale_summary_json(work_dir, stem, src_lang).exists()
            else None,
        },
    )

    want = locales
    if want is None:
        want = [tag for tag in srt_map if tag != src_tag]

    for loc in want:
        loc_n = normalize_lang(loc)
        if loc_n == src_lang:
            continue
        tag = file_tag(loc_n)
        srt = srt_map.get(tag) or locale_srt_path(work_dir, loc_n, stem)
        loc_summary_path = locale_summary_json(work_dir, stem, loc_n)
        if not loc_summary_path.exists():
            loc_summary_path = work_dir / f"{stem}_summary_{tag}.json"
        loc_sum = _load_summary(loc_summary_path) if loc_summary_path.exists() else {}
        loc_fields = notes_fields(loc_sum or summary)
        if not loc_fields["title"]:
            loc_fields["title"] = summary.get("title") or title_src
        content.upsert_locale(
            article_id,
            loc_n,
            {
                **loc_fields,
                "srt_path": str(srt) if srt.exists() else None,
                "txt_path": None,
                "summary_json_path": str(loc_summary_path)
                if loc_summary_path.exists()
                else (
                    str(locale_summary_json(work_dir, stem, src_lang))
                    if locale_summary_json(work_dir, stem, src_lang).exists()
                    else None
                ),
            },
        )
    content._conn.execute(
        "DELETE FROM article_locales WHERE article_id=? AND locale IN ('src','auto','source')",
        (article_id,),
    )
    content._conn.commit()
    return article_id


def _process_media_only(
    job,
    *,
    queue: QueueDB,
    content: ContentDB,
    work_dir: Path,
    enabled: frozenset[str],
    frame_mode: str | None,
    gif_duration: float | None,
    frame_clips: list | None,
) -> None:
    """Re-cut GIF and/or MP4 ranges on an existing work dir (no ASR)."""
    job_id = int(job["id"])
    platform = job["platform"]
    video_id = job["video_id"]
    url = job["canonical_url"] or job["url"]
    topic_id = job["topic_id"] or "home_tips"
    out_dir = work_dir
    wav = existing_wav(work_dir)

    summary = load_notes_summary(out_dir, "full_16k", "zh")
    if not summary:
        summary = load_notes_summary(out_dir, "full_16k")
    needs_notes = "frames" in enabled
    if needs_notes and not summary:
        raise FileNotFoundError(
            f"stages={sorted(enabled)} needs existing notes in {work_dir}"
        )

    src_lang = coalesce_source_lang(
        str((summary or {}).get("source_lang") or "zh")
    )
    print(f"[batch] media-only stages={sorted(enabled)}")
    if "dehardsub" in enabled:
        from media_ops import strip_hardsubs

        opts = _job_media_opts(job)
        strip_hardsubs(
            out_dir,
            force=bool(opts.get("dehardsub_force")),
            ratio=float(opts.get("dehardsub_ratio") or 0.14),
            mode=str(opts.get("dehardsub_mode") or "auto"),
            vlm_model=str(opts.get("dehardsub_vlm_model") or ""),
            passes=int(opts.get("dehardsub_passes") or 2),
            demosaic=bool(opts.get("dehardsub_demosaic", True)),
            engine=str(opts.get("dehardsub_engine") or "sttn"),
        )
    if "frames" in enabled or "clips" in enabled:
        summary = _attach_job_media(
            work_dir=out_dir,
            job=job,
            summary=summary,
            src_lang=src_lang,
            enabled=enabled,
            frame_mode=frame_mode,
            gif_duration=gif_duration,
            frame_clips=frame_clips,
        ) or summary
    _run_job_postproc(out_dir, job, enabled, frame_clips=frame_clips)

    article_id = None
    if summary:
        article_id = register_work_dir(
            content,
            platform=platform,
            video_id=video_id,
            topic_id=topic_id,
            canonical_url=url,
            work_dir=out_dir,
            source_lang=src_lang,
            title=summary.get("title") or job["title"],
            author=job["author"],
            published_at=job["published_at"],
            views=job["views"],
            duration_sec=job["duration_sec"],
            thumb_url=job["thumb_url"],
            source_wav=str(wav) if wav else _job_field(job, "source_wav"),
        )
    queue.mark_done(
        job_id,
        source_wav=str(wav) if wav else _job_field(job, "source_wav"),
        canonical_url=url,
    )
    print(f"[batch] done job#{job_id} article_id={article_id} (media-only)")


def process_job(
    job,
    *,
    queue: QueueDB,
    content: ContentDB,
    work_root: Path,
    langs: str,
    source_lang: str,
    chat_model: str,
    translate_model: str,
    device: str,
    multipass: bool,
    llm_correct: bool,
    skip_translate: bool,
    skip_summary: bool,
    skip_keypoints: bool,
    localize_summary: bool,
    max_attempts: int,
    dry_run: bool,
    skip_fetch: bool = False,
    frame_mode: str | None = None,
    gif_duration: float | None = None,
    frame_clips: list | None = None,
    lang_workers_n: int | None = None,
    stages: str = "all",
) -> None:
    job_id = int(job["id"])
    platform = job["platform"]
    video_id = job["video_id"]
    url = job["canonical_url"] or job["url"]
    topic_id = job["topic_id"]
    work_dir = _job_work_dir(work_root, platform, video_id)
    enabled = parse_stages(stages)

    print(f"[batch] job#{job_id} {platform}:{video_id} topic={topic_id} stages={sorted(enabled)}")
    if dry_run:
        print(f"[batch] dry-run → would process into {work_dir}")
        # undo claim so job stays pending
        import time as _time

        queue._conn.execute(
            """
            UPDATE jobs
            SET status='pending',
                attempts=CASE WHEN attempts>0 THEN attempts-1 ELSE 0 END,
                updated_at=?
            WHERE id=?
            """,
            (_time.time(), job_id),
        )
        queue._conn.commit()
        return

    # Media-only: GIF/MP4/enhance/compress/concat without ASR.
    if enabled and enabled <= MEDIA_ONLY_STAGES:
        _process_media_only(
            job,
            queue=queue,
            content=content,
            work_dir=work_dir,
            enabled=enabled,
            frame_mode=frame_mode,
            gif_duration=gif_duration,
            frame_clips=frame_clips,
        )
        return

    from fetch_media import detect_bvid, fetch_to_wav

    need_wav = ("fetch" in enabled) or ("asr" in enabled)
    wav = existing_wav(work_dir)
    if need_wav:
        if skip_fetch or "fetch" not in enabled:
            if not wav or not is_usable_wav(wav):
                raise FileNotFoundError(
                    f"stages need wav but none in {work_dir}"
                    if "fetch" not in enabled
                    else f"upload job missing wav in {work_dir}"
                )
            print(
                f"[fetch] upload local wav -> {wav}"
                if skip_fetch
                else f"[fetch] reuse local wav -> {wav}"
            )
        elif wav and is_usable_wav(wav):
            print(f"[fetch] reuse local wav -> {wav}")
        else:
            meta = fetch_to_wav(
                url,
                work_dir,
                cookies_from_browser="chrome",
                force=False,
            )
            wav = Path(meta["wav"])
        queue.set_source_wav(job_id, str(wav))
    else:
        if wav and is_usable_wav(wav):
            print(f"[fetch] optional reuse -> {wav}")
            queue.set_source_wav(job_id, str(wav))
        else:
            wav = Path(_job_field(job, "source_wav")) if _job_field(job, "source_wav") else work_dir / "media" / "full_16k.wav"
            print("[fetch] skipped (stages omit fetch/asr)")

    args = SimpleNamespace(
        cmd="run",
        video=wav,
        out_dir=work_dir,
        source_lang=source_lang,
        summary_lang=source_lang if source_lang != "auto" else "zh",
        langs=langs,
        target_lang=None,
        ollama_model=translate_model,
        chat_model=chat_model,
        correct_model=chat_model,
        device=device,
        compute_type="int8" if device == "cpu" else "float16",
        whisper_model=str(LOCAL_WHISPER),
        multipass=multipass,
        relisten=multipass,
        llm_correct=llm_correct,
        llm_all=False,
        skip_translate=skip_translate,
        skip_summary=skip_summary,
        skip_keypoints=skip_keypoints,
        reuse_audio=True,
        force=True,
        auto=False,
        from_srt=None,
        from_txt=None,
        hotwords=None,
        hotwords_file=None,
        bvid=detect_bvid(url) if platform == "bilibili" else None,
        slice_start_sec=0.0,
        sync_shift_ms=0,
        no_cookies=False,
        cookies_from_browser="chrome",
        cookies=None,
        lang_workers=lang_workers(lang_workers_n),
    )

    ollama = ""
    try:
        from llm.factory import get_role_config

        if any(get_role_config(r).provider == "ollama" for r in ("chat", "translate")):
            ollama = find_ollama()
    except Exception:
        try:
            ollama = find_ollama()
        except Exception:
            ollama = ""

    glossary: dict = {}
    need_notes = ("notes" in enabled) and (
        (not skip_summary) or (skip_summary and not skip_keypoints)
    )
    need_segs = need_notes or ((not skip_translate) and ("translate" in enabled))

    if "asr" in enabled:
        segs, out_dir, stem, _srt = transcribe_video(args)
        src_lang = coalesce_source_lang(
            args.source_lang if args.source_lang != "auto" else "en"
        )
        src_srt = _find_source_srt(out_dir, stem, src_lang)
        if src_srt:
            src_lang = _detect_source_lang(args.source_lang, src_srt)

        if ollama and llm_correct:
            try:
                ensure_ollama_model(chat_model, role="chat")
                glossary = auto_build_glossary(ollama, chat_model, segs, src_lang)
                gloss_path = job_media_dir(out_dir) / "glossary.json"
                gloss_path.write_text(
                    json.dumps(glossary, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                print(f"[out] {gloss_path}")
                segs = correct_segments(ollama, chat_model, segs, glossary, src_lang)
                src_srt = locale_srt_path(out_dir, src_lang, stem)
                write_srt(segs, src_srt)
                print(f"[auto] corrected source srt -> {src_srt}")
            except Exception as e:
                print(f"[auto] glossary/correct skipped ({type(e).__name__}: {e})")
                glossary = {}
    elif need_segs:
        segs, out_dir, stem, src_lang, _src_srt = _load_existing_job_segments(
            work_dir, source_lang=source_lang
        )
        glossary = _load_existing_glossary(out_dir)
        if glossary:
            print(f"[glossary] reuse {len(glossary)} entries")
    else:
        # localize / media extras without re-translate: notes + optional SRT lang detect
        out_dir = work_dir
        stem = "full_16k"
        segs = []
        summary_probe = load_notes_summary(out_dir, stem, "zh") or load_notes_summary(
            out_dir, stem
        )
        if summary_probe and summary_probe.get("source_lang"):
            src_lang = coalesce_source_lang(str(summary_probe["source_lang"]))
        else:
            src_srt = _find_source_srt(out_dir, stem, source_lang)
            src_lang = _detect_source_lang(source_lang, src_srt)
        print(f"[stages] skip asr/translate/notes — reuse work_dir lang={src_lang}")

    if not skip_translate and "translate" in enabled:
        ensure_ollama_model(translate_model, role="translate")
    if need_notes:
        ensure_ollama_model(chat_model, role="chat")

    args.source_lang = src_lang
    do_tr = (not skip_translate) and ("translate" in enabled)
    do_sum = need_notes and (not skip_summary)
    do_kp = need_notes and skip_summary and (not skip_keypoints)

    def _notes_branch() -> None:
        args.summary_lang = src_lang
        if do_sum:
            do_summary(args, ollama, segments_plain_text(segs), out_dir, stem)
        elif do_kp:
            do_keypoints(args, ollama, segments_plain_text(segs), out_dir, stem)

    def _translate_branch() -> None:
        do_translate(args, ollama, segs, out_dir, stem, glossary)

    # After source SRT is stable: fork source notes ∥ subtitle translate.
    if do_tr and (do_sum or do_kp):
        print("[batch] fork: source notes ∥ subtitle translate")
        errors: list[tuple[str, BaseException]] = []
        with ThreadPoolExecutor(max_workers=2) as pool:
            futs = {
                pool.submit(_translate_branch): "translate",
                pool.submit(_notes_branch): "notes",
            }
            for fut in as_completed(futs):
                name = futs[fut]
                try:
                    fut.result()
                except BaseException as e:
                    errors.append((name, e))
                    locked_print(f"[batch FAIL] {name}: {type(e).__name__}: {e}")
        if errors:
            name0, err0 = errors[0]
            raise RuntimeError(f"fork failed ({name0}): {err0}") from err0
    elif do_tr:
        _translate_branch()
    elif do_sum or do_kp:
        _notes_branch()

    summary = load_notes_summary(out_dir, stem, src_lang)

    need_targets = (not skip_translate) and (
        "translate" in enabled or "localize" in enabled
    )
    targets = resolve_targets(langs, src_lang) if need_targets else []
    if localize_summary and "localize" in enabled:
        if not summary:
            raise FileNotFoundError(
                f"localize needs existing source notes in {out_dir / 'notes'}"
            )
        if not targets:
            print("[notes-loc] no target locales — skipped")
        else:
            ensure_ollama_model(translate_model, role="translate")
            workers = lang_workers(lang_workers_n)
            print(f"[notes-loc] {len(targets)} locales (workers={workers})")

            def _one_loc(loc: str) -> Path:
                loc_data = translate_summary_locale(
                    ollama,
                    translate_model,
                    summary,
                    source_lang=src_lang,
                    target_lang=loc,
                )
                loc_data["source_lang"] = src_lang
                loc_data["summary_lang"] = loc
                _js, md = write_locale_notes(loc_data, out_dir, loc)
                locked_print(f"[out] {md}")
                return md

            if workers <= 1 or len(targets) <= 1:
                for loc in targets:
                    _one_loc(loc)
            else:
                errors = []
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = {pool.submit(_one_loc, loc): loc for loc in targets}
                    for fut in as_completed(futures):
                        loc = futures[fut]
                        try:
                            fut.result()
                        except BaseException as e:
                            errors.append((loc, e))
                            locked_print(
                                f"[notes-loc FAIL] {loc}: {type(e).__name__}: {e}"
                            )
                if errors:
                    loc0, err0 = errors[0]
                    raise RuntimeError(
                        f"notes localize failed for {len(errors)} locale(s); "
                        f"first={loc0}: {err0}"
                    ) from err0
            write_notes_index(out_dir)

    # Optional screenshots for visual how-tos (needs source video in media/)
    if "frames" in enabled or "clips" in enabled:
        try:
            summary = _attach_job_media(
                work_dir=out_dir,
                job=job,
                summary=summary,
                src_lang=src_lang,
                enabled=enabled,
                frame_mode=frame_mode,
                gif_duration=gif_duration,
                frame_clips=frame_clips,
            )
        except Exception as e:
            print(f"[frames] FAILED ({type(e).__name__}: {e})")
            traceback.print_exc()

    try:
        _run_job_postproc(out_dir, job, enabled, frame_clips=frame_clips)
    except Exception as e:
        print(f"[postproc] FAILED ({type(e).__name__}: {e})")
        traceback.print_exc()

    wav_path = existing_wav(out_dir) or (
        Path(wav) if wav and Path(wav).is_file() else None
    )
    article_id = register_work_dir(
        content,
        platform=platform,
        video_id=video_id,
        topic_id=topic_id,
        canonical_url=url,
        work_dir=out_dir,
        source_lang=src_lang,
        title=(summary or {}).get("title") or job["title"],
        author=job["author"],
        published_at=job["published_at"],
        views=job["views"],
        duration_sec=job["duration_sec"],
        thumb_url=job["thumb_url"],
        source_wav=str(wav_path) if wav_path else _job_field(job, "source_wav"),
        locales=targets or None,
    )
    queue.mark_done(
        job_id,
        source_wav=str(wav_path) if wav_path else _job_field(job, "source_wav"),
        canonical_url=url,
    )
    print(f"[batch] done job#{job_id} article_id={article_id}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Process pending discover queue → content.db")
    p.add_argument("--db", type=Path, default=None, help="queue.db path")
    p.add_argument("--content-db", type=Path, default=None, help="content.db path")
    p.add_argument(
        "--work-dir",
        type=Path,
        default=ROOT / "downloads" / "batch",
        help="per-job work root",
    )
    p.add_argument("--limit", type=int, default=1, help="max jobs this run (default 1)")
    p.add_argument("--langs", default="site", help="translate pack (default site)")
    p.add_argument(
        "--lang-workers",
        type=int,
        default=None,
        help=(
            "Parallel languages for subtitle translate + notes localize (1–8). "
            f"Default from VITUAL_LANG_WORKERS or {DEFAULT_LANG_WORKERS}"
        ),
    )
    p.add_argument("--source-lang", default="auto")
    p.add_argument("--chat-model", default=DEFAULT_CHAT)
    p.add_argument("--ollama-model", default=DEFAULT_TRANSLATE)
    p.add_argument(
        "--llm-profile",
        default=None,
        help="LLM profile: local|tokenhub|openai|hybrid (see llm.example.yaml)",
    )
    p.add_argument("--llm-config", type=Path, default=None)
    p.add_argument("--device", default="cpu", choices=("cpu", "cuda"))
    p.add_argument("--no-multipass", action="store_true")
    p.add_argument("--no-llm-correct", action="store_true")
    p.add_argument("--skip-translate", action="store_true")
    p.add_argument("--skip-summary", action="store_true")
    p.add_argument(
        "--skip-keypoints",
        action="store_true",
        help="skip LLM bullet key-point extraction",
    )
    p.add_argument(
        "--no-localize-summary",
        action="store_true",
        help="do not translate summary JSON per locale",
    )
    p.add_argument(
        "--stages",
        default="all",
        help=(
            "Pipeline stages: all | llm | post | media | clips | frames | "
            "enhance | compress | concat | "
            "or comma list (fetch,asr,...,enhance). "
            "all does not include enhance/compress/concat. "
            "Omit asr → reuse existing SRT; clips/media → re-cut only."
        ),
    )
    p.add_argument("--max-attempts", type=int, default=3)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--requeue-failed", action="store_true")
    p.add_argument(
        "--register",
        type=Path,
        default=None,
        help="Only register an existing work_dir into content.db (no ASR)",
    )
    p.add_argument("--platform", default="youtube")
    p.add_argument("--video-id", default=None)
    p.add_argument("--topic", default="inbox")
    p.add_argument("--url", default=None)
    p.add_argument("--title", default=None)
    args = p.parse_args(argv)

    content = ContentDB(args.content_db)
    queue = QueueDB(args.db)

    from llm.factory import configure_llm

    if not args.register:
        configure_llm(
            profile=args.llm_profile,
            config_path=args.llm_config,
            chat_model=args.chat_model,
            translate_model=args.ollama_model,
        )

    try:
        if args.register:
            work = args.register.resolve()
            if not work.is_dir():
                print(f"error: not a directory: {work}", file=sys.stderr)
                return 1
            vid = args.video_id or work.name
            url = args.url or (
                f"https://www.youtube.com/watch?v={vid}"
                if args.platform == "youtube"
                else f"https://www.bilibili.com/video/{vid}"
            )
            aid = register_work_dir(
                content,
                platform=args.platform,
                video_id=vid,
                topic_id=args.topic,
                canonical_url=url,
                work_dir=work,
                source_lang=coalesce_source_lang(
                    args.source_lang if args.source_lang != "auto" else "en"
                ),
                title=args.title,
            )
            print(f"[register] article_id={aid} content={content.path}")
            print(f"[register] locales={len(content.list_locales(aid))}")
            return 0

        if args.requeue_failed:
            n = queue.requeue_failed()
            print(f"[queue] requeued failed={n}")

        print(f"[queue] {queue.path} status={queue.count_by_status()}")
        print(f"[content] {content.path} articles={content.count_articles()}")

        done = 0
        for _ in range(max(0, args.limit)):
            job = queue.claim_next(
                max_attempts=args.max_attempts, video_id=args.video_id
            )
            if job is None:
                print("[batch] no pending jobs")
                break
            try:
                process_job(
                    job,
                    queue=queue,
                    content=content,
                    work_root=args.work_dir.resolve(),
                    langs=args.langs,
                    source_lang=args.source_lang,
                    chat_model=args.chat_model,
                    translate_model=args.ollama_model,
                    device=args.device,
                    multipass=not args.no_multipass,
                    llm_correct=not args.no_llm_correct,
                    skip_translate=args.skip_translate,
                    skip_summary=args.skip_summary,
                    skip_keypoints=args.skip_keypoints,
                    localize_summary=not args.no_localize_summary,
                    max_attempts=args.max_attempts,
                    dry_run=args.dry_run,
                    lang_workers_n=args.lang_workers,
                    stages=args.stages,
                )
                done += 1
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                print(f"[batch] FAIL job#{job['id']}: {err}", file=sys.stderr)
                traceback.print_exc()
                attempts = int(job["attempts"]) + 1  # already incremented on claim
                # claim already bumped attempts; read fresh
                fresh = queue.get_job(int(job["id"]))
                att = int(fresh["attempts"]) if fresh else attempts
                queue.mark_failed(
                    int(job["id"]),
                    err + "\n" + traceback.format_exc()[-800:],
                    dead=att >= args.max_attempts,
                )

        print(f"[batch] processed={done} queue={queue.count_by_status()}")
        print(f"[content] articles={content.count_articles()}")
        return 0
    finally:
        queue.close()
        content.close()


if __name__ == "__main__":
    raise SystemExit(main())
