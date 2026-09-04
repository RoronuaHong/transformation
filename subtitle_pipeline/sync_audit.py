"""Full-chain subtitle sync audit for a job work_dir or site derived slug."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from media_ops import (
    CANONICAL_REMIX_FILES,
    caption_events_for_body,
    caption_clock_for_body,
    probe_duration_sec,
    probe_leading_silence_sec,
)
from pipeline import parse_srt
from sync_utils import assert_timeline_aligned, validate_srt_monotonic

ROOT = Path(__file__).resolve().parent
DEFAULT_WORK = ROOT / "downloads" / "batch" / "youtube_kV7RuutRx-s"
DEFAULT_SLUG = "understanding-artificial-intelligence-and-machine-learning-basics"
DEFAULT_EXPORT = ROOT.parent / "transform" / "content" / "articles.json"


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

    src_srt = None
    for cand in (subs / "en.srt", subs / "zh.srt"):
        if cand.is_file():
            src_srt = cand
            break
    if src_srt is None and subs.is_dir():
        srts = sorted(subs.glob("*.srt"))
        src_srt = srts[0] if srts else None
    if src_srt is None:
        fail("S1-source-srt", "no subs/*.srt")
        return out

    # --- S1 / S8 sync_meta ---
    if sync_meta.is_file():
        try:
            sm = json.loads(sync_meta.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            sm = {}
        slice0 = float(sm.get("slice_start_sec") or 0)
        shift = int(sm.get("sync_shift_ms") or 0)
        if abs(slice0) < 0.01:
            if sm.get("merged_from_slices"):
                ok(
                    "S1-merged-slices",
                    f"parts={len(sm.get('slice_merge_parts') or sm.get('parts') or [])}",
                )
            else:
                ok("S1-full-wav-asr", f"slice_start_sec={slice0}")
        else:
            warn("S1-slice-offset", f"slice_start_sec={slice0} (clips ASR on full embed?)")

        # Detect unmerged multi-slice ASR leftovers (S1 footgun).
        clips_meta = media / "clips" / "clips_meta.json"
        if clips_meta.is_file() and not sm.get("merged_from_slices"):
            try:
                from merge_slices import discover_clip_parts

                found = discover_clip_parts(work, clock="source")
            except Exception:
                found = []
            if len(found) >= 2:
                warn(
                    "S1-unmerged-clip-srts",
                    f"{len(found)} range SRTs present — run yarn merge-slices --work …",
                )
        if shift == 0:
            ok("S8-no-global-shift", "sync_shift_ms=0 (download clock)")
        elif str(sm.get("reason") or "") == "embed_ads" or sm.get("embed_shift_ms"):
            ok(
                "S8-embed-shift",
                f"sync_shift_ms={shift} reason={sm.get('reason') or 'embed_ads'}",
            )
        else:
            warn("S8-global-shift", f"sync_shift_ms={shift}")
    else:
        warn("sync_meta", "missing media/sync_meta.json")

    # --- S6 monotonic ---
    try:
        validate_srt_monotonic(parse_srt(src_srt))
        ok("S6-monotonic", src_srt.name)
    except Exception as e:
        fail("S6-monotonic", str(e))

    # --- locale SRT alignment ---
    bad = []
    for other in sorted(subs.glob("*.srt")):
        if other.resolve() == src_srt.resolve():
            continue
        try:
            assert_timeline_aligned(parse_srt(src_srt), parse_srt(other))
        except Exception as e:
            bad.append(f"{other.name}: {e}")
    if bad:
        fail("S7-translate-axis", "; ".join(bad[:3]))
    else:
        ok("S7-translate-axis", f"all locales match {src_srt.name}")

    # --- overlay remix ---
    if cues_json.is_file():
        try:
            cues = json.loads(cues_json.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as e:
            fail("remix_cues", str(e))
            cues = {}
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
        silence = probe_leading_silence_sec(remix) if remix.is_file() else None
        if silence is not None and abs(silence - intro) <= 0.08:
            ok("S10-intro-silence", f"intro={intro:.3f}s silence_end={silence:.3f}s")
        elif silence is not None:
            warn("S10-intro-silence", f"intro={intro:.3f}s vs silence={silence:.3f}s")
        elif remix.is_file():
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
                    ok(
                        "S10-first-caption",
                        f"clips-clock drift={float(first_cap['start']) - float(events[0][0]):+.3f}s",
                    )
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

        if vtt.is_file():
            vtt_text = vtt.read_text(encoding="utf-8")
            if caps and caps[0]["text"][:20] in vtt_text:
                ok("vtt-json", "first caption in vtt")
            else:
                warn("vtt-json", "vtt may not match json cues")
    elif remix.is_file():
        fail("remix_cues", "remix.mp4 present but remix_cues.json missing")

    # Prefer remix_meta caption_clock when overlay was built from clips.
    meta = media / "remix" / "remix_meta.json"
    if meta.is_file():
        try:
            rm = json.loads(meta.read_text(encoding="utf-8-sig"))
            cc = str(rm.get("caption_clock") or "").strip().lower()
            if cc in {"clips", "source"}:
                out["caption_clock"] = cc
                if cc == "clips":
                    spans_path = media / "clips" / "clips_meta.json"
                    if spans_path.is_file():
                        ok("S3-clips-clock", "remix_meta caption_clock=clips")
                    else:
                        warn("S3-clips-clock", "clips clock but no clips_meta.json")
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass
    if "caption_clock" not in out:
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

    # --- duration sanity ---
    remix_d = probe_duration_sec(remix) if remix.is_file() else None
    body_d = probe_duration_sec(body) if body.is_file() else None
    if remix_d and body_d:
        gap = remix_d - body_d
        intro = float(out.get("overlay", {}).get("intro_sec") or 0)
        if abs(gap - intro) <= 0.12:
            ok("S3-remix-body-gap", f"gap={gap:.3f}s intro={intro:.3f}s")
        elif intro > gap + 0.12 and (intro - gap) <= 1.2:
            # silence_end includes body leading hush; overlay correctly uses silence.
            ok(
                "S3-remix-body-gap",
                f"gap={gap:.3f}s intro={intro:.3f}s (body leading silence OK)",
            )
        elif intro > 0 and abs(gap - intro) <= 0.55:
            ok("S3-remix-body-gap", f"gap={gap:.3f}s intro={intro:.3f}s (AAC skew OK)")
        else:
            warn("S3-remix-body-gap", f"gap={gap:.3f}s intro={intro:.3f}s")

    remix_dir = remix.parent if remix.is_file() else media / "remix"
    if remix_dir.is_dir():
        stale = [
            p.name
            for p in remix_dir.iterdir()
            if p.is_file()
            and p.name.startswith("remix_")
            and p.name not in CANONICAL_REMIX_FILES
            and not p.name.startswith("_")
        ]
        if stale:
            warn("S10-locale-remix-stale", f"remove unused: {stale[:8]}")
        elif cues_json.is_file() or remix.is_file():
            ok("S10-canonical-remix-only", "no remix_<lang> leftovers")

    status_path = media / "media_status.json"
    if status_path.is_file():
        try:
            ms = json.loads(status_path.read_text(encoding="utf-8"))
            out["media_status"] = ms
            if ms.get("postproc") == "failed" or ms.get("remix") == "failed":
                fail("media_status", str(ms.get("error") or ms))
            elif ms.get("remix") == "ok":
                ok("media_status", f"remix=ok postproc={ms.get('postproc')}")
            else:
                ok(
                    "media_status",
                    f"postproc={ms.get('postproc')} remix={ms.get('remix')}",
                )
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
            warn("media_status", f"unreadable: {e}")

    # --- publish 3-point spot-check (start/mid/end) ---
    try:
        from spot_check import spot_check_work_dir

        spot = spot_check_work_dir(work, check_energy=True)
        out["spot"] = {
            "source_points": (spot.get("source") or {}).get("points"),
            "remix_points": (spot.get("remix") or {}).get("points"),
        }
        for row in spot.get("checks") or []:
            ok(str(row.get("name") or "spot"), str(row.get("detail") or ""))
        for row in spot.get("warnings") or []:
            warn(str(row.get("name") or "spot"), str(row.get("detail") or ""))
        for row in spot.get("failures") or []:
            fail(str(row.get("name") or "spot"), str(row.get("detail") or ""))
    except Exception as e:
        warn("spot-check", f"{type(e).__name__}: {e}")

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
    clock = art.get("cue_clock")
    if clock == "source":
        checks.append({"status": "pass", "name": "N8-cue-clock", "detail": "cue_clock=source"})
    elif clock:
        warnings.append({"name": "N8-cue-clock", "detail": f"cue_clock={clock}"})
    else:
        warnings.append({"name": "N8-cue-clock", "detail": "missing cue_clock (re-export)"})
    remix = art.get("remix")
    if isinstance(remix, dict) and remix.get("video"):
        if remix.get("clock") == "remix":
            checks.append({"status": "pass", "name": "N8-remix-clock", "detail": "article.remix.clock=remix"})
        else:
            warnings.append({"name": "N8-remix-clock", "detail": f"remix={remix.get('clock')}"})
    if len(locales) >= 16:
        checks.append({"status": "pass", "name": "N8-notes-locales", "detail": f"{len(locales)} locales"})
    derived = ROOT.parent / "transform" / "public" / "derived" / slug
    for name in ("remix.mp4", "remix_cues.json", "remix.vtt"):
        if (derived / name).is_file():
            checks.append({"status": "pass", "name": f"derived-{name}", "detail": "present"})
        else:
            warnings.append({"name": f"derived-{name}", "detail": "missing"})
    return {"slug": slug, "checks": checks, "warnings": warnings, "failures": failures}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Subtitle sync audit (work_dir + articles.json)")
    p.add_argument("work", nargs="?", type=Path, default=None, help="job work_dir")
    p.add_argument("slug", nargs="?", default=None, help="site slug for export/derived checks")
    p.add_argument("--export", type=Path, default=DEFAULT_EXPORT, help="articles.json path")
    p.add_argument(
        "--strict",
        action="store_true",
        help="fail if default smoke work_dir is missing",
    )
    p.add_argument(
        "--require-remix",
        action="store_true",
        help="fail when work_dir has no remix.mp4 / remix_cues.json",
    )
    p.add_argument(
        "--skip-export",
        action="store_true",
        help="only audit work_dir",
    )
    args = p.parse_args(argv)

    work = Path(args.work) if args.work else DEFAULT_WORK
    slug = args.slug or DEFAULT_SLUG

    if not work.is_dir():
        msg = f"[sync:audit] work_dir missing: {work}"
        if args.strict:
            print(msg, file=sys.stderr)
            return 2
        print(f"{msg} (skip; pass --strict to fail)")
        return 0

    work_report = audit_work_dir(work)
    if args.require_remix:
        remix = work / "media" / "remix" / "remix.mp4"
        cues = work / "media" / "remix" / "remix_cues.json"
        if not remix.is_file():
            work_report.setdefault("failures", []).append(
                {"name": "require-remix", "detail": "remix.mp4 missing"}
            )
        if not cues.is_file():
            work_report.setdefault("failures", []).append(
                {"name": "require-remix", "detail": "remix_cues.json missing"}
            )

    report: dict = {"work": work_report}
    if not args.skip_export:
        if args.export.is_file():
            report["export"] = audit_export(args.export, slug)
        else:
            report["export"] = {
                "failures": [{"name": "export", "detail": f"missing {args.export}"}],
                "warnings": [],
                "checks": [],
            }

    print(json.dumps(report, ensure_ascii=False, indent=2))
    n_fail = len(report["work"].get("failures") or [])
    if "export" in report:
        n_fail += len(report["export"].get("failures") or [])
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
