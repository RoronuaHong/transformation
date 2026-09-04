"""Publish-time 3-point caption spot-check (start / mid / end).

Does not re-run ASR. Validates cue placement against media duration and, when
ffmpeg is available, flags cues that land in near-silence (likely S4/S10 drift).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from fetch_media import find_ffmpeg
from media_ops import probe_duration_sec
from pipeline import parse_srt

ROOT = Path(__file__).resolve().parent
_MEAN_VOL_RE = re.compile(r"mean_volume:\s*([-\d.]+)\s*dB", re.I)
_MAX_VOL_RE = re.compile(r"max_volume:\s*([-\d.]+)\s*dB", re.I)


def pick_three_points(segments: list[dict]) -> list[tuple[str, dict]]:
    """Return labeled (start|mid|end) cues from a segment list."""
    segs = [s for s in segments if str(s.get("text") or "").strip()]
    if not segs:
        return []
    if len(segs) == 1:
        return [("start", segs[0])]
    if len(segs) == 2:
        return [("start", segs[0]), ("end", segs[-1])]
    mid = len(segs) // 2
    return [("start", segs[0]), ("mid", segs[mid]), ("end", segs[-1])]


def probe_window_volume_db(
    media: Path,
    t_sec: float,
    *,
    window: float = 0.35,
) -> tuple[float | None, float | None]:
    """Return (mean_db, max_db) for [t, t+window) via ffmpeg volumedetect."""
    if not media.is_file():
        return None, None
    try:
        ffmpeg = find_ffmpeg()
    except FileNotFoundError:
        return None, None
    start = max(0.0, float(t_sec))
    dur = max(0.08, float(window))
    try:
        r = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-ss",
                f"{start:.3f}",
                "-t",
                f"{dur:.3f}",
                "-i",
                str(media),
                "-af",
                "volumedetect",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=45,
        )
    except Exception:
        return None, None
    blob = f"{r.stderr or ''}\n{r.stdout or ''}"
    mean_m = _MEAN_VOL_RE.search(blob)
    max_m = _MAX_VOL_RE.search(blob)
    mean_db = float(mean_m.group(1)) if mean_m else None
    max_db = float(max_m.group(1)) if max_m else None
    return mean_db, max_db


def _cue_bounds(seg: dict) -> tuple[float, float, str]:
    start = float(seg["start"])
    end = float(seg["end"])
    text = str(seg.get("text") or "").strip().replace("\n", " ")
    return start, end, text


def check_point_in_duration(
    label: str,
    seg: dict,
    duration: float | None,
    *,
    tol: float = 0.35,
) -> dict:
    start, end, text = _cue_bounds(seg)
    row: dict = {
        "point": label,
        "start": round(start, 3),
        "end": round(end, 3),
        "text": text[:80],
        "status": "pass",
        "detail": "",
    }
    if end <= start:
        row["status"] = "fail"
        row["detail"] = "end<=start"
        return row
    if duration is None or duration <= 0.05:
        row["status"] = "warn"
        row["detail"] = "media duration unknown"
        return row
    if start >= duration + tol:
        row["status"] = "fail"
        row["detail"] = f"start={start:.2f} past media={duration:.2f}"
        return row
    if end > duration + tol:
        row["status"] = "warn"
        row["detail"] = f"end={end:.2f} past media={duration:.2f}"
        return row
    row["detail"] = f"within {duration:.2f}s"
    return row


def check_point_energy(
    label: str,
    seg: dict,
    media: Path,
    *,
    silence_mean_db: float = -42.0,
    speech_max_db: float = -32.0,
) -> dict | None:
    """Warn when a caption lands in near-silence (possible lip/ASR drift).

    Samples inside the cue (not at the raw start) because Whisper often opens
    slightly before audible speech.
    """
    start, end, text = _cue_bounds(seg)
    span = max(0.05, end - start)
    sample_t = start + min(0.28, span * 0.35)
    mean_db, max_db = probe_window_volume_db(media, sample_t)
    if mean_db is None:
        return None
    row = {
        "point": label,
        "start": round(start, 3),
        "sample_t": round(sample_t, 3),
        "text": text[:80],
        "mean_db": mean_db,
        "max_db": max_db,
        "status": "pass",
        "detail": f"@ {sample_t:.2f}s mean={mean_db:.1f}dB",
    }
    # Speech often has low mean but a clear max peak.
    if max_db is not None and max_db >= speech_max_db:
        row["detail"] = f"@ {sample_t:.2f}s max={max_db:.1f}dB"
        return row
    if mean_db <= silence_mean_db:
        row["status"] = "warn"
        row["detail"] = (
            f"near-silence @ {sample_t:.2f}s mean={mean_db:.1f}dB "
            f"max={max_db if max_db is not None else 'n/a'} (check lip sync)"
        )
    return row


def spot_check_segments(
    segments: list[dict],
    *,
    media: Path | None = None,
    duration: float | None = None,
    check_energy: bool = True,
) -> dict:
    points = pick_three_points(segments)
    out: dict = {
        "points": [],
        "energy": [],
        "checks": [],
        "warnings": [],
        "failures": [],
    }
    if not points:
        out["failures"].append({"name": "spot-empty", "detail": "no cue text"})
        return out

    if duration is None and media is not None:
        duration = probe_duration_sec(media)

    for label, seg in points:
        row = check_point_in_duration(label, seg, duration)
        out["points"].append(row)
        name = f"spot-{label}-bounds"
        if row["status"] == "fail":
            out["failures"].append({"name": name, "detail": row["detail"]})
        elif row["status"] == "warn":
            out["warnings"].append({"name": name, "detail": row["detail"]})
        else:
            out["checks"].append({"status": "pass", "name": name, "detail": row["detail"]})

    if check_energy and media is not None and media.is_file():
        for label, seg in points:
            erow = check_point_energy(label, seg, media)
            if erow is None:
                continue
            out["energy"].append(erow)
            name = f"spot-{label}-energy"
            if erow["status"] == "warn":
                out["warnings"].append({"name": name, "detail": erow["detail"]})
            else:
                out["checks"].append(
                    {"status": "pass", "name": name, "detail": erow["detail"]}
                )

    labels = ",".join(p for p, _ in points)
    if not out["failures"]:
        out["checks"].insert(
            0,
            {
                "status": "pass",
                "name": "spot-three-points",
                "detail": f"sampled {labels} (n={len(segments)})",
            },
        )
    return out


def _find_source_media(work: Path) -> Path | None:
    media = work / "media"
    for name in ("full_16k.wav", "source.mp4", "source.webm", "source.mkv"):
        p = media / name
        if p.is_file():
            return p
    wavs = sorted(media.glob("*.wav")) if media.is_dir() else []
    return wavs[0] if wavs else None


def _find_source_srt(work: Path) -> Path | None:
    subs = work / "subs"
    for cand in (subs / "en.srt", subs / "zh.srt"):
        if cand.is_file():
            return cand
    if not subs.is_dir():
        return None
    srts = sorted(subs.glob("*.srt"))
    return srts[0] if srts else None


def spot_check_work_dir(work: Path, *, check_energy: bool = True) -> dict:
    """Run start/mid/end checks on source SRT and remix overlay captions."""
    work = Path(work)
    report: dict = {
        "work_dir": str(work),
        "source": None,
        "remix": None,
        "checks": [],
        "warnings": [],
        "failures": [],
    }

    def _merge(prefix: str, part: dict) -> None:
        for key in ("checks", "warnings", "failures"):
            for row in part.get(key) or []:
                item = dict(row)
                item["name"] = f"{prefix}:{item.get('name')}"
                report[key].append(item)

    srt = _find_source_srt(work)
    media = _find_source_media(work)
    if srt is None:
        report["failures"].append({"name": "source:srt", "detail": "no subs/*.srt"})
    else:
        segs = parse_srt(srt)
        src = spot_check_segments(
            segs, media=media, check_energy=check_energy and media is not None
        )
        src["srt"] = srt.name
        src["media"] = media.name if media else None
        report["source"] = src
        _merge("source", src)

    cues_path = work / "media" / "remix" / "remix_cues.json"
    remix_mp4 = work / "media" / "remix" / "remix.mp4"
    if cues_path.is_file():
        try:
            payload = json.loads(cues_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as e:
            report["failures"].append({"name": "remix:cues", "detail": str(e)})
            payload = {}
        caps = [
            {"start": c["start"], "end": c["end"], "text": c.get("text") or ""}
            for c in (payload.get("cues") or [])
            if c.get("kind") == "caption"
        ]
        remix = spot_check_segments(
            caps,
            media=remix_mp4 if remix_mp4.is_file() else None,
            check_energy=check_energy and remix_mp4.is_file(),
        )
        intro = float(payload.get("intro_sec") or 0)
        if caps and intro > 0:
            first = float(caps[0]["start"])
            name = "remix:first-after-intro"
            if first + 0.05 >= intro - 0.05 and first <= intro + 3.0:
                report["checks"].append(
                    {
                        "status": "pass",
                        "name": name,
                        "detail": f"first={first} intro={intro:.3f}",
                    }
                )
            elif first < intro - 0.05:
                report["failures"].append(
                    {
                        "name": name,
                        "detail": f"first={first} before intro={intro:.3f}",
                    }
                )
            else:
                report["warnings"].append(
                    {"name": name, "detail": f"first={first} intro={intro:.3f}"}
                )
        remix["intro_sec"] = intro
        report["remix"] = remix
        _merge("remix", remix)

    return report


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="3-point caption spot-check for a work_dir")
    p.add_argument("work", nargs="?", type=Path, default=ROOT / "downloads" / "batch" / "youtube_kV7RuutRx-s")
    p.add_argument("--no-energy", action="store_true", help="skip ffmpeg volumedetect")
    args = p.parse_args(argv)
    if not args.work.is_dir():
        print(f"[spot] missing work_dir: {args.work}", file=sys.stderr)
        return 2
    report = spot_check_work_dir(args.work, check_energy=not args.no_energy)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report.get("failures") else 0


if __name__ == "__main__":
    raise SystemExit(main())
