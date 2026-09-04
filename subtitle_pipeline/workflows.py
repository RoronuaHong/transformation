"""Independent workflow task expansion (W0).

Each selected module becomes its own queue job (workflow key) sharing a pack_id /
work_dir. Discover/batch keeps workflow=\"all\".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# Canonical workflow ids (product WF map).
WORKFLOW_TRANSLATE = "translate"
WORKFLOW_NOTES = "notes"
WORKFLOW_CLIPS = "clips"
WORKFLOW_FRAMES = "frames"
WORKFLOW_DEHARDSUB = "dehardsub"
WORKFLOW_DEBLUR = "deblur"
WORKFLOW_ENHANCE = "enhance"
WORKFLOW_COMPRESS = "compress"
WORKFLOW_CONCAT = "concat"
WORKFLOW_REMIX = "remix"
WORKFLOW_PUBLISH = "publish"
WORKFLOW_ALL = "all"
INPUT_SOURCE = "source"
INPUT_AUTO = "auto"

# Run order when multiple are selected in one submit.
WORKFLOW_ORDER: tuple[str, ...] = (
    WORKFLOW_TRANSLATE,
    WORKFLOW_NOTES,
    WORKFLOW_DEHARDSUB,
    WORKFLOW_CLIPS,
    WORKFLOW_FRAMES,
    WORKFLOW_CONCAT,
    WORKFLOW_DEBLUR,
    WORKFLOW_ENHANCE,
    WORKFLOW_COMPRESS,
    WORKFLOW_REMIX,
    WORKFLOW_PUBLISH,
)

# Video handoff chain (earlier → later). REQ-A03 auto-serial.
VIDEO_INPUT_CHAIN: tuple[str, ...] = (
    WORKFLOW_DEHARDSUB,
    WORKFLOW_CONCAT,
    WORKFLOW_DEBLUR,
    WORKFLOW_ENHANCE,
    WORKFLOW_COMPRESS,
    WORKFLOW_REMIX,
)

# Relative paths under work_dir for pack artifacts.
WORKFLOW_ARTIFACT_RELS: dict[str, tuple[str, ...]] = {
    INPUT_SOURCE: ("media/source.mp4", "media/source.mkv", "media/source.webm"),
    WORKFLOW_DEHARDSUB: ("media/dehardsub/clean.mp4",),
    WORKFLOW_CLIPS: (),  # glob media/clips/range_*.mp4
    WORKFLOW_CONCAT: ("media/concat/concat.mp4",),
    WORKFLOW_DEBLUR: ("media/deblur/deblurred.mp4",),
    WORKFLOW_ENHANCE: ("media/enhance/enhanced.mp4",),
    WORKFLOW_COMPRESS: ("media/compress/compressed.mp4",),
    WORKFLOW_REMIX: ("media/remix/remix.mp4",),
    WORKFLOW_PUBLISH: ("media/publish/report.json",),
    WORKFLOW_TRANSLATE: ("subs",),
    WORKFLOW_NOTES: ("notes",),
}


def pack_id_for(platform: str, video_id: str) -> str:
    return f"{platform}_{video_id}"


def expand_workflow_tasks(
    *,
    want_translate: bool = False,
    want_notes: bool = False,
    want_clips: bool = False,
    want_frames: bool = False,
    want_dehardsub: bool = False,
    want_deblur: bool = False,
    want_enhance: bool = False,
    want_compress: bool = False,
    want_concat: bool = False,
    want_remix: bool = False,
    want_publish: bool = False,
    stages: str | None = None,
    has_source_srt: bool = False,
    input_from: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Map module toggles → ordered tasks with stages / depends_on / input_from."""
    toggles: dict[str, bool] = {
        WORKFLOW_TRANSLATE: bool(want_translate),
        WORKFLOW_NOTES: bool(want_notes),
        WORKFLOW_CLIPS: bool(want_clips) or bool(want_concat),
        WORKFLOW_FRAMES: bool(want_frames),
        WORKFLOW_DEHARDSUB: bool(want_dehardsub),
        WORKFLOW_DEBLUR: bool(want_deblur),
        WORKFLOW_ENHANCE: bool(want_enhance),
        WORKFLOW_COMPRESS: bool(want_compress),
        WORKFLOW_CONCAT: bool(want_concat),
        WORKFLOW_REMIX: bool(want_remix),
        WORKFLOW_PUBLISH: bool(want_publish),
    }
    any_toggle = any(toggles.values())
    if not any_toggle:
        raw = (stages or "").strip()
        if not raw or raw.lower() == "all":
            tasks: list[dict[str, Any]] = [
                {"workflow": WORKFLOW_ALL, "stages": "all"}
            ]
        else:
            first = raw.split(",")[0].strip().lower() or "custom"
            tasks = [{"workflow": first, "stages": raw}]
        return annotate_task_handoffs(tasks, input_from=input_from)

    out: list[dict[str, Any]] = []
    if toggles[WORKFLOW_TRANSLATE]:
        if has_source_srt:
            out.append({"workflow": WORKFLOW_TRANSLATE, "stages": "translate"})
        else:
            out.append(
                {"workflow": WORKFLOW_TRANSLATE, "stages": "fetch,asr,translate"}
            )
    if toggles[WORKFLOW_NOTES]:
        if toggles[WORKFLOW_TRANSLATE] or has_source_srt:
            out.append({"workflow": WORKFLOW_NOTES, "stages": "notes,localize"})
        else:
            out.append(
                {"workflow": WORKFLOW_NOTES, "stages": "fetch,asr,notes,localize"}
            )
    if toggles[WORKFLOW_DEHARDSUB]:
        out.append({"workflow": WORKFLOW_DEHARDSUB, "stages": "dehardsub"})
    if toggles[WORKFLOW_CLIPS]:
        out.append({"workflow": WORKFLOW_CLIPS, "stages": "clips"})
    if toggles[WORKFLOW_FRAMES]:
        out.append({"workflow": WORKFLOW_FRAMES, "stages": "frames"})
    if toggles[WORKFLOW_CONCAT]:
        out.append({"workflow": WORKFLOW_CONCAT, "stages": "concat"})
    if toggles[WORKFLOW_DEBLUR]:
        out.append({"workflow": WORKFLOW_DEBLUR, "stages": "deblur"})
    if toggles[WORKFLOW_ENHANCE]:
        out.append({"workflow": WORKFLOW_ENHANCE, "stages": "enhance"})
    if toggles[WORKFLOW_COMPRESS]:
        out.append({"workflow": WORKFLOW_COMPRESS, "stages": "compress"})
    if toggles[WORKFLOW_REMIX]:
        out.append({"workflow": WORKFLOW_REMIX, "stages": "remix"})
    if toggles[WORKFLOW_PUBLISH]:
        out.append({"workflow": WORKFLOW_PUBLISH, "stages": "publish"})
    return annotate_task_handoffs(out, input_from=input_from)


def default_input_from(workflow: str, selected: set[str]) -> str | None:
    """Pick upstream video (or clips) artifact key for ``workflow``."""
    wf = (workflow or "").strip()
    if wf in (WORKFLOW_TRANSLATE, WORKFLOW_NOTES, WORKFLOW_FRAMES, WORKFLOW_ALL):
        return None
    if wf == WORKFLOW_CLIPS:
        return WORKFLOW_DEHARDSUB if WORKFLOW_DEHARDSUB in selected else INPUT_SOURCE
    if wf == WORKFLOW_CONCAT:
        return WORKFLOW_CLIPS
    if wf == WORKFLOW_DEHARDSUB:
        return INPUT_SOURCE
    if wf == WORKFLOW_PUBLISH:
        for cand in reversed(VIDEO_INPUT_CHAIN):
            if cand in selected:
                return cand
        return INPUT_SOURCE
    if wf in VIDEO_INPUT_CHAIN:
        idx = VIDEO_INPUT_CHAIN.index(wf)
        for prev in reversed(VIDEO_INPUT_CHAIN[:idx]):
            if prev in selected:
                return prev
        return INPUT_SOURCE
    return INPUT_SOURCE


def annotate_task_handoffs(
    tasks: list[dict[str, Any]],
    *,
    input_from: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Attach ``depends_on`` + ``input_from`` (REQ-A03 auto + overrides)."""
    overrides = {
        str(k).strip(): str(v).strip()
        for k, v in (input_from or {}).items()
        if str(k).strip() and str(v).strip()
    }
    selected = {str(t.get("workflow") or "") for t in tasks}
    annotated: list[dict[str, Any]] = []
    for raw in tasks:
        t = dict(raw)
        wf = str(t.get("workflow") or "")
        inp = overrides.get(wf) or default_input_from(wf, selected)
        if inp == INPUT_AUTO:
            inp = default_input_from(wf, selected)
        deps: list[str] = []
        if wf == WORKFLOW_NOTES and WORKFLOW_TRANSLATE in selected:
            deps.append(WORKFLOW_TRANSLATE)
        if inp and inp not in (INPUT_SOURCE, INPUT_AUTO, None) and inp in selected:
            if inp not in deps:
                deps.append(inp)
        # frames after notes when both selected
        if wf == WORKFLOW_FRAMES and WORKFLOW_NOTES in selected:
            if WORKFLOW_NOTES not in deps:
                deps.append(WORKFLOW_NOTES)
        t["input_from"] = inp
        t["depends_on"] = deps
        annotated.append(t)
    return annotated


def tasks_meta_payload(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """JSON-serializable task list for pack responses."""
    out: list[dict[str, Any]] = []
    for t in tasks:
        row: dict[str, Any] = {
            "workflow": t["workflow"],
            "stages": t["stages"],
        }
        if t.get("input_from") is not None:
            row["input_from"] = t["input_from"]
        if t.get("depends_on"):
            row["depends_on"] = list(t["depends_on"])
        out.append(row)
    return out


def artifact_path_for(work_dir: Path, key: str) -> Path | None:
    """Resolve a single handoff key to an existing file (or None)."""
    from job_layout import job_media_dir
    from note_frames import existing_source_video

    k = (key or "").strip() or INPUT_SOURCE
    root = Path(work_dir)
    if k in (INPUT_SOURCE, INPUT_AUTO):
        return existing_source_video(root)
    if k == WORKFLOW_CLIPS:
        clips = sorted((job_media_dir(root) / "clips").glob("range_*.mp4"))
        usable = [p for p in clips if p.is_file() and p.stat().st_size > 800]
        return usable[0] if usable else None
    rels = WORKFLOW_ARTIFACT_RELS.get(k) or ()
    for rel in rels:
        p = root / rel
        if p.is_file() and p.stat().st_size > 0:
            if p.suffix.lower() == ".json" or p.stat().st_size > 800:
                return p
    return None


def list_pack_artifacts(work_dir: Path) -> list[dict[str, Any]]:
    """List available handoff artifacts under a pack work_dir."""
    from job_layout import job_media_dir
    from note_frames import existing_source_video

    root = Path(work_dir)
    items: list[dict[str, Any]] = []
    src = existing_source_video(root)
    if src is not None:
        items.append(
            {
                "key": INPUT_SOURCE,
                "label": "source",
                "path": str(src),
                "bytes": src.stat().st_size,
            }
        )
    for key, rels in WORKFLOW_ARTIFACT_RELS.items():
        if key == INPUT_SOURCE:
            continue
        if key == WORKFLOW_CLIPS:
            clips_dir = job_media_dir(root) / "clips"
            ranges = sorted(clips_dir.glob("range_*.mp4"))
            for p in ranges:
                if p.is_file() and p.stat().st_size > 800:
                    items.append(
                        {
                            "key": WORKFLOW_CLIPS,
                            "label": p.name,
                            "path": str(p),
                            "bytes": p.stat().st_size,
                        }
                    )
            continue
        if key in (WORKFLOW_TRANSLATE, WORKFLOW_NOTES):
            d = root / rels[0]
            if d.is_dir() and any(d.iterdir()):
                items.append(
                    {
                        "key": key,
                        "label": key,
                        "path": str(d),
                        "bytes": None,
                    }
                )
            continue
        for rel in rels:
            p = root / rel
            if not p.is_file():
                continue
            if p.suffix.lower() != ".json" and p.stat().st_size < 800:
                continue
            items.append(
                {
                    "key": key,
                    "label": p.name,
                    "path": str(p),
                    "bytes": p.stat().st_size,
                }
            )
            break
    return items


# Each WF owns a subdirectory (contract). Shared pack root = work_dir.
WORKFLOW_HOME: dict[str, str] = {
    WORKFLOW_TRANSLATE: "subs",
    WORKFLOW_NOTES: "notes",
    WORKFLOW_CLIPS: "media/clips",
    WORKFLOW_FRAMES: "media/frames",
    WORKFLOW_DEHARDSUB: "media/dehardsub",
    WORKFLOW_DEBLUR: "media/deblur",
    WORKFLOW_ENHANCE: "media/enhance",
    WORKFLOW_COMPRESS: "media/compress",
    WORKFLOW_CONCAT: "media/concat",
    WORKFLOW_REMIX: "media/remix",
    WORKFLOW_PUBLISH: "media/publish",
}


def workflow_home(work_dir: Path, workflow: str) -> Path:
    """Return (and create) the canonical product directory for one WF."""
    rel = WORKFLOW_HOME.get((workflow or "").strip())
    root = Path(work_dir)
    if not rel:
        d = root / "media" / "workflows" / (workflow or "custom")
    else:
        d = root / rel
    d.mkdir(parents=True, exist_ok=True)
    return d


def workflow_products(work_dir: Path, workflow: str) -> list[dict[str, Any]]:
    """List primary product files belonging to one workflow."""
    from job_layout import job_media_dir, list_locale_srts

    root = Path(work_dir)
    wf = (workflow or "").strip()
    out: list[dict[str, Any]] = []

    def _add(path: Path, *, kind: str) -> None:
        if not path.is_file():
            return
        try:
            size = path.stat().st_size
        except OSError:
            return
        if path.suffix.lower() not in (".json", ".vtt", ".srt", ".md", ".txt") and size < 800:
            return
        if path.suffix.lower() in (".json", ".vtt", ".srt", ".md", ".txt") and size < 8:
            return
        out.append(
            {
                "name": path.name,
                "kind": kind,
                "path": str(path),
                "bytes": size,
                "rel": str(path.relative_to(root)).replace("\\", "/"),
            }
        )

    if wf == WORKFLOW_TRANSLATE:
        for tag, p in sorted(list_locale_srts(root).items()):
            _add(p, kind=f"srt:{tag}")
    elif wf == WORKFLOW_NOTES:
        notes = root / "notes"
        if notes.is_dir():
            for p in sorted(notes.glob("*/summary.json")):
                _add(p, kind=f"notes:{p.parent.name}")
            for p in sorted(notes.glob("*/summary.md")):
                _add(p, kind=f"notes_md:{p.parent.name}")
    elif wf == WORKFLOW_CLIPS:
        for p in sorted((job_media_dir(root) / "clips").glob("range_*.mp4")):
            _add(p, kind="clip")
    elif wf == WORKFLOW_FRAMES:
        frames = job_media_dir(root) / "frames"
        if frames.is_dir():
            for p in sorted(frames.glob("*"))[:20]:
                _add(p, kind="frame")
    elif wf in WORKFLOW_ARTIFACT_RELS:
        hit = artifact_path_for(root, wf)
        if hit is not None:
            _add(hit, kind=wf)
        # sibling meta
        home = workflow_home(root, wf)
        for meta in home.glob("*_meta.json"):
            _add(meta, kind="meta")
        for extra in ("remix_cues.json", "remix.vtt", "report.json"):
            _add(home / extra, kind="sidecar")
    return out


def infer_workflow_progress(
    workflow: str,
    status: str,
    work_dir: Path,
    *,
    active: bool = False,
) -> dict[str, Any]:
    """Progress for a single WF job (not the whole pack)."""
    wf = (workflow or "").strip() or WORKFLOW_ALL
    st = (status or "").strip().lower()
    if st in ("done", "published"):
        return {"percent": 100, "stage": wf, "detail": None, "active": False}
    if st in ("failed", "dead"):
        return {"percent": 0, "stage": "failed", "detail": None, "active": False}
    if st == "cancelled":
        return {"percent": 0, "stage": "cancelled", "detail": None, "active": False}
    if st == "pending":
        return {
            "percent": 0,
            "stage": "queued",
            "detail": None,
            "active": False,
        }

    # processing
    products = workflow_products(work_dir, wf)
    if products:
        return {
            "percent": 85 if active else 70,
            "stage": wf,
            "detail": str(len(products)),
            "active": active,
        }
    # stage-specific soft progress
    soft = {
        WORKFLOW_TRANSLATE: ("translate", 40),
        WORKFLOW_NOTES: ("notes", 45),
        WORKFLOW_CLIPS: ("clips", 35),
        WORKFLOW_FRAMES: ("frames", 35),
        WORKFLOW_DEHARDSUB: ("dehardsub", 40),
        WORKFLOW_DEBLUR: ("deblur", 40),
        WORKFLOW_ENHANCE: ("enhance", 40),
        WORKFLOW_COMPRESS: ("compress", 40),
        WORKFLOW_CONCAT: ("concat", 40),
        WORKFLOW_REMIX: ("remix", 50),
        WORKFLOW_PUBLISH: ("publish", 30),
        WORKFLOW_ALL: ("run", 30),
    }
    stage, pct = soft.get(wf, (wf or "run", 25))
    return {
        "percent": pct if active else max(5, pct // 2),
        "stage": stage,
        "detail": None,
        "active": active,
    }


def write_pack_manifest(
    work_dir: Path,
    *,
    pack_id: str,
    tasks: list[dict[str, Any]],
) -> Path:
    """Persist per-WF status + products under media/pack_manifest.json."""
    import json
    import time

    from job_layout import job_media_dir

    root = Path(work_dir)
    enriched: list[dict[str, Any]] = []
    for t in tasks:
        wf = str(t.get("workflow") or "")
        row = {
            "workflow": wf,
            "job_id": t.get("job_id"),
            "status": t.get("status"),
            "input_from": t.get("input_from"),
            "depends_on": list(t.get("depends_on") or []),
            "home": WORKFLOW_HOME.get(wf),
            "products": workflow_products(root, wf),
        }
        enriched.append(row)
    payload = {
        "pack_id": pack_id,
        "updated_at": time.time(),
        "tasks": enriched,
        "artifacts": list_pack_artifacts(root),
    }
    dest = job_media_dir(root) / "pack_manifest.json"
    dest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return dest
