"""WF-08 one-click publish: local_stage ledger (no unofficial platform upload)."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from job_layout import job_media_dir
from publish_accounts import (
    PUBLISH_PLATFORMS,
    get_record,
    normalize_platform,
    valid_bound,
)

PUBLISH_MODE = "local_stage"


class PublishError(ValueError):
    """Missing video or no bound publish accounts."""


def job_publish_dir(work_dir: Path) -> Path:
    d = job_media_dir(work_dir) / "publish"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def resolve_publish_video(work_dir: Path) -> Path | None:
    from media_ops import resolve_working_video

    return resolve_working_video(work_dir)


def build_caption(work_dir: Path) -> dict[str, str]:
    from media_ops import load_note_hook

    hook = load_note_hook(work_dir)
    title = (hook.get("one_liner") or hook.get("title") or "").strip()
    points: list[str] = []
    notes = Path(work_dir) / "notes"
    lang = hook.get("lang") or "zh"
    summary = notes / lang / "summary.json"
    if not summary.is_file() and notes.is_dir():
        for p in notes.glob("*/summary.json"):
            summary = p
            break
    if summary.is_file():
        try:
            data = json.loads(summary.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            data = {}
        if isinstance(data, dict):
            if not title:
                title = str(data.get("title") or "").strip()
            for row in data.get("key_points") or []:
                if isinstance(row, dict):
                    t = str(row.get("title") or "").strip()
                    if t:
                        points.append(t)
                if len(points) >= 4:
                    break
    if not title:
        title = Path(work_dir).name
    body = title
    if points:
        body = title + "\n" + "\n".join(f"· {p}" for p in points)
    return {"title": title[:80], "body": body[:2000], "tags": " ".join(points[:8])}


def _item(
    *,
    platform: str,
    account_id: str,
    status: str,
    url: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "platform": platform,
        "account_id": account_id,
        "status": status,
        "url": url,
        "error": error,
        "at": _now(),
        "mode": PUBLISH_MODE,
    }


def _load_report(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"items": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {"items": []}
    if not isinstance(data, dict):
        return {"items": []}
    items = data.get("items")
    if not isinstance(items, list):
        data["items"] = []
    return data


def _merge_items(
    existing: list[dict[str, Any]], incoming: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_plat: dict[str, dict[str, Any]] = {}
    for row in existing:
        if isinstance(row, dict) and row.get("platform"):
            by_plat[str(row["platform"])] = row
    for row in incoming:
        by_plat[str(row["platform"])] = row
    order = list(PUBLISH_PLATFORMS)
    extra = [p for p in by_plat if p not in order]
    return [by_plat[p] for p in (*order, *extra) if p in by_plat]


def _stage_platform(work_dir: Path, rec: dict[str, Any], video: Path, caption: dict) -> dict:
    plat = str(rec["platform"])
    dest_dir = job_publish_dir(work_dir) / plat
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "video.mp4"
    shutil.copy2(video, dest)
    (dest_dir / "caption.txt").write_text(caption["body"], encoding="utf-8")
    (dest_dir / "meta.json").write_text(
        json.dumps(
            {
                "platform": plat,
                "account_id": rec.get("account_id") or plat,
                "title": caption["title"],
                "mode": PUBLISH_MODE,
                "src": video.name,
                "bytes": dest.stat().st_size,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    rel = dest.as_posix()
    return _item(
        platform=plat,
        account_id=str(rec.get("account_id") or plat),
        status="ok",
        url=rel,
        error=None,
    )


def run_publish(
    work_dir: Path,
    *,
    platforms: list[str] | None = None,
    retry_platform: str | None = None,
) -> dict[str, Any]:
    """Stage remix/source to bound valid accounts. Unbound platforms are skipped.

    v1 does **not** upload to Douyin/Kuaishou servers (no official third-party API).
    """
    work_dir = Path(work_dir)
    video = resolve_publish_video(work_dir)
    if video is None or not video.is_file() or video.stat().st_size < 800:
        raise PublishError(f"publish needs a video in {work_dir}")

    bound = {s["platform"]: s for s in valid_bound()}
    if retry_platform:
        wanted = [normalize_platform(retry_platform)]
    elif platforms:
        wanted = []
        for raw in platforms:
            try:
                wanted.append(normalize_platform(raw))
            except ValueError:
                continue
        wanted = list(dict.fromkeys(wanted))
    else:
        wanted = [p for p in PUBLISH_PLATFORMS if p in bound]

    if not bound:
        raise PublishError("no bound publish accounts")

    caption = build_caption(work_dir)
    incoming: list[dict[str, Any]] = []
    for plat in wanted:
        slot = bound.get(plat)
        if slot is None:
            incoming.append(
                _item(
                    platform=plat,
                    account_id="",
                    status="skipped",
                    error="unbound or invalid",
                )
            )
            continue
        rec = get_record(plat) or {}
        incoming.append(_stage_platform(work_dir, rec, video, caption))

    report_path = job_publish_dir(work_dir) / "report.json"
    prev = _load_report(report_path)
    items = _merge_items(list(prev.get("items") or []), incoming)
    payload = {
        "mode": PUBLISH_MODE,
        "src": video.name,
        "title": caption["title"],
        "items": items,
        "at": _now(),
    }
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    ok = bool(items) and all(row.get("status") == "ok" for row in incoming)
    return {"ok": ok, "report": report_path, "items": incoming}
