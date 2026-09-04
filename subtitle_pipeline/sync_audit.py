"""Full-chain subtitle sync audit for a job work_dir or site derived slug."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from media_ops import (
    caption_events_for_body,
    caption_clock_for_body,
    overlay_intro_sec,
    probe_duration_sec,
    probe_leading_silence_sec,
    probe_stream_duration_sec,
)
from pipeline import parse_srt
from segment_subs import remap_cues_to_spans
from sync_utils import assert_timeline_aligned, validate_srt_monotonic


def _parse_ts(s: str) -> float:
    s = str(s).strip().replace(",", ".")
    parts = s.split(":")
    if len(parts) != 3:
        return 0.0
    h, m, sec = int(parts[0]), int(parts[1]), float(parts[2])
    return h * 3600 + m * 60 + sec


def audit_work_dir(work: Path) -> dict:
    work = Path(work)
    media = work / "media"
    remix = media / "remix" / "remix.mp4"
    body = media / "remix" / "_body.mp4"
    cues_json = media / "remix" / "remix_cues.json"
    vtt = media / "remix" / "remix.vtt"
    sync_meta = media / "sync_meta.json"
    subs = work / "subs"

    out: dict = {"work_dir": str(work), "checks": [], "warnings": [], "failures": []}

    def ok(name: str, detail: str = "") -> None:
        out["checks"].append({"status": "pass", "name": name, "detail": detail})

    def warn(name: str, detail: str) -> None:
        out["warnings"].append({"name": name, "detail": detail})

    def fail(name: str, detail: str) -> None:
        out["failures"].append({"name": name, "detail": detail})

    # --- ASR meta ---
    if sync_meta.is_file():
        meta = json.loads(sync_meta.read_text(encoding="utf-8"))
        out["sync_meta"] = meta
        if float(meta.get("slice_start_sec") or 0) != 0:
            warn("S1-slice-offset", f"slice_start_sec={meta.get('slice_start_sec')}")
        else:
            ok("S1-full-wav-asr", "slice_start_sec=0")
        if int(meta.get("sync_shift_ms") or 0) != 0:
            warn("S8-shift", f"sync_shift_ms={meta.get('sync_shift_ms')}")
        else:
            ok("S8-no-global-shift", "sync_shift_ms=0")
    else:
        fail("sync_meta", "missing sync_meta.json")

    # --- locale SRT alignment ---
    src_srt = subs / "en.srt"
    if not src_srt.is_file():
        for p in sorted(subs.glob("*.srt")):
            src_srt = p
            break
    if src_srt.is_file():
        src_segs = parse_srt(src_srt)
        mono = validate_srt_monotonic(src_segs)
        if mono:
            warn("S6-monotonic", "; ".join(mono[:3]))
        else:
            ok("S6-monotonic", f"{len(src_segs)} cues monotonic")
        for loc in sorted(subs.glob("*.srt")):
            if loc == src_srt:
                continue
            tgt = parse_srt(loc)
            try:
                assert_timeline_aligned(src_segs, tgt, label=loc.stem)
            except ValueError as e:
                fail("S7-translate-axis", f"{loc.name}: {e}")
        if not any(f["name"] == "S7-translate-axis" for f in out["failures"]):
            ok("S7-translate-axis", f"all locales match {src_srt.name}")
    else:
        warn("subs", "no SRT found")

    # --- remix overlay ---
    if not remix.is_file():
        warn("remix", "no remix.mp4 — skip overlay audit")
        return out

    if cues_json.is_file():
        cues = json.loads(cues_json.read_text(encoding="utf-8"))
        out["overlay"] = {
            "audio_clock": cues.get("audio_clock"),
            "intro_sec": cues.get("intro_sec"),
            "clock": cues.get("clock"),
            "source_clock": cues.get("source_clock"),
            "ncues": len(cues.get("cues") or []),
        }
        if cues.get("audio_clock") is True:
            ok("S10-audio-clock-flag", "audio_clock=true")
        else:
            fail("S10-audio-clock-flag", f"audio_clock={cues.get('audio_clock')}")

        intro = float(cues.get("intro_sec") or 0)
        silence = probe_leading_silence_sec(remix)
        if silence is not None and abs(silence - intro) <= 0.08:
            ok("S10-intro-silence", f"intro={intro:.3f}s silence_end={silence:.3f}s")
        elif silence is not None:
            warn("S10-intro-silence", f"intro={intro:.3f}s vs silence={silence:.3f}s")
        else:
            warn("S10-intro-silence", "silencedetect found no leading silence")

        caps = [c for c in cues.get("cues") or [] if c.get("kind") == "caption"]
        if caps and src_srt.is_file():
            first_cap = caps[0]
            source_clock = str(cues.get("source_clock") or "source")
            if source_clock == "clips":
                body_guess = body if body.is_file() else remix
                events = caption_events_for_body(
                    work, src_srt, body=body_guess, shift=intro, clock="clips"
                )
                if events and abs(float(first_cap["start"]) - float(events[0][0])) <= 0.05:
                    ok("S10-first-caption", f"clips-clock drift={float(first_cap['start'])-float(events[0][0]):+.3f}s")
                elif events:
                    fail(
                        "S10-first-caption",
                        f"cap={first_cap['start']} remapped≈{events[0][0]:.3f}",
                    )
                else:
                    warn("S10-first-caption", "clips clock but no remapped events")
            else:
                first_src = parse_srt(src_srt)[0]
                expected = intro + float(first_src["start"])
                drift = float(first_cap["start"]) - expected
                if abs(drift) <= 0.05:
                    ok("S10-first-caption", f"drift={drift:+.3f}s")
                else:
                    fail(
                        "S10-first-caption",
                        f"cap={first_cap['start']} expected≈{expected:.3f} drift={drift:+.3f}s",
                    )
            last_end = float(caps[-1]["end"])
            remix_d = probe_duration_sec(remix) or 0
            if remix_d and last_end > remix_d + 0.2:
                fail("S10-cues-past-end", f"last_end={last_end:.2f} remix={remix_d:.2f}")
            elif remix_d:
                ok("S10-cues-within", f"last_end={last_end:.2f} remix={remix_d:.2f}")

        # vtt vs json
        if vtt.is_file():
            vtt_text = vtt.read_text(encoding="utf-8")
            if caps and caps[0]["text"][:20] in vtt_text:
                ok("vtt-json", "first caption in vtt")
            else:
                warn("vtt-json", "vtt may not match json cues")
    else:
        fail("remix_cues", "missing remix_cues.json")

    # --- body clock for clips path ---
    src_video = None
    for name in ("source.mp4", "source.webm", "_src_av.mp4"):
        p = media / name
        if p.is_file():
            src_video = p
            break
    body_for_clock = body if body.is_file() else (src_video or remix)
    if src_srt.is_file() and body_for_clock.is_file():
        clock = caption_clock_for_body(work, body_for_clock)
        out["caption_clock"] = clock
        if clock == "clips":
            spans_path = media / "clips" / "clips_meta.json"
            if spans_path.is_file():
                ok("S3-clips-clock", "body uses clips clock with clips_meta")
            else:
                warn("S3-clips-clock", "clips clock but no clips_meta.json")

    # --- duration sanity (silence-based intro is authoritative; video gap can lag AAC) ---
    remix_d = probe_duration_sec(remix)
    body_d = probe_duration_sec(body) if body.is_file() else None
    if remix_d and body_d:
        gap = remix_d - body_d
        intro = float(out.get("overlay", {}).get("intro_sec") or 0)
        if abs(gap - intro) <= 0.12:
            ok("S3-remix-body-gap", f"gap={gap:.3f}s intro={intro:.3f}s")
        elif intro > 0 and abs(gap - intro) <= 0.55:
            # AAC priming often shortens container gap vs silence_end; cues use silence.
            ok("S3-remix-body-gap", f"gap={gap:.3f}s intro={intro:.3f}s (AAC skew OK)")
        else:
            warn("S3-remix-body-gap", f"gap={gap:.3f}s intro={intro:.3f}s")

    return out


def audit_export(articles_path: Path, slug: str) -> dict:
    articles_path = Path(articles_path)
    data = json.loads(articles_path.read_text(encoding="utf-8"))
    art = next((a for a in data.get("articles") or [] if a.get("slug") == slug), None)
    if not art:
        return {"failures": [{"name": "export", "detail": f"slug {slug} not in articles.json"}]}
    cues = art.get("cues") or []
    locales = art.get("locales") or {}
    failures: list[dict] = []
    warnings: list[dict] = []
    checks: list[dict] = []
    if len(cues) < 1:
        failures.append({"name": "N8-cues", "detail": "articles.json cues empty"})
    else:
        checks.append({"status": "pass", "name": "N8-cues", "detail": f"{len(cues)} cues"})
        nlang = len(cues[0].get("text") or {})
        if nlang >= 16:
            checks.append({"status": "pass", "name": "N8-16lang", "detail": f"{nlang} text locales"})
        else:
            warnings.append({"name": "N8-16lang", "detail": f"only {nlang} text locales in cue[0]"})
    if len(locales) >= 16:
        checks.append({"status": "pass", "name": "N8-notes-locales", "detail": f"{len(locales)} locales"})
    derived = Path(__file__).resolve().parent.parent / "transform" / "public" / "derived" / slug
    for name in ("remix.mp4", "remix_cues.json", "remix.vtt"):
        if (derived / name).is_file():
            checks.append({"status": "pass", "name": f"derived-{name}", "detail": "present"})
        else:
            warnings.append({"name": f"derived-{name}", "detail": "missing"})
    return {"slug": slug, "checks": checks, "warnings": warnings, "failures": failures}


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    root = Path(__file__).resolve().parent
    work = root / "downloads" / "batch" / "youtube_kV7RuutRx-s"
    slug = "understanding-artificial-intelligence-and-machine-learning-basics"
    if argv:
        if Path(argv[0]).is_dir():
            work = Path(argv[0])
        if len(argv) > 1:
            slug = argv[1]
    report = {"work": audit_work_dir(work), "export": audit_export(root.parent / "transform" / "content" / "articles.json", slug)}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    n_fail = len(report["work"].get("failures") or []) + len(report["export"].get("failures") or [])
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
