"""Audit remix overlay sync: probe durations vs cue map vs source SRT."""
from __future__ import annotations

import json
from pathlib import Path

from media_ops import caption_events_for_body, clip_span_durations, probe_duration_sec
from pipeline import parse_srt
from segment_subs import remap_cues_to_spans

work = Path("downloads/batch/bilibili_BV1M34y1P76V")
media = work / "media"
clips_dir = media / "clips"
body = media / "compress" / "compressed.mp4"
remix = media / "remix" / "remix.mp4"
srt = work / "subs" / "zh.srt"
cues_json = json.loads((media / "remix" / "remix_cues.json").read_text(encoding="utf-8"))
meta = json.loads((media / "clips" / "clips_meta.json").read_text(encoding="utf-8"))
intro = float(cues_json.get("intro_sec") or 0)

spans = [(float(r["start"]), float(r["end"])) for r in meta["spans"]]
nominal = [e - s for s, e in spans]
probed_clips = [probe_duration_sec(clips_dir / f"range_{i:02d}.mp4") for i in range(len(spans))]
probed_body = probe_duration_sec(body)
probed_remix = probe_duration_sec(remix)
probed_intro = probe_duration_sec(media / "remix" / "_intro.mp4")

segs = parse_srt(srt)
remapped_nominal = remap_cues_to_spans(segs, spans)
remapped_probed = remap_cues_to_spans(segs, spans, durations=probed_clips)
events_intro = caption_events_for_body(work, srt, body=body, shift=intro)

print("=== durations ===")
print("spans", spans)
print("nominal clip durs", [round(x, 3) for x in nominal])
print("probed clip durs", [round(x or 0, 3) for x in probed_clips])
print("sum nominal", round(sum(nominal), 3), "sum probed", round(sum(x or 0 for x in probed_clips), 3))
print("body", round(probed_body or 0, 3), "intro", round(probed_intro or 0, 3), "remix", round(probed_remix or 0, 3))
print("expected remix", round(intro + (probed_body or 0), 3))

print("\n=== stitch boundary (remix clock) ===")
for i, d in enumerate(probed_clips):
    if d:
        print(f"  clip{i} ends ~{intro + sum(probed_clips[: i + 1]):.3f}s")

print("\n=== boundary cue gap (saved remix_cues) ===")
caps = [c for c in cues_json["cues"] if c["kind"] == "caption"]
for i in range(len(caps) - 1):
    gap = caps[i + 1]["start"] - caps[i]["end"]
    if abs(gap) > 0.02:
        print(
            f"  gap {gap:+.3f}s between",
            repr(caps[i]["text"][:18]),
            "->",
            repr(caps[i + 1]["text"][:18]),
        )

print("\n=== remap drift at 2nd clip (probed vs nominal) ===")
by_text = {r["text"]: r for r in remapped_probed}
for row in remapped_nominal:
    t = row["text"]
    if t not in by_text:
        continue
    drift = by_text[t]["start"] - row["start"]
    if abs(drift) > 0.02:
        print(f"  {drift:+.3f}s @ {t[:24]}")

print("\n=== key lines: source vs overlay start (remix clock) ===")
keys = [
    "因为他们往里面加了几种特别的东西",
    "我们平时在家蒸米饭",
    "淘洗大米也是非常有讲究的",
    "加入30度左右的温水",
]
src_by = {str(s["text"]).strip(): s for s in segs}
ovl_by = {c["text"]: c for c in cues_json["cues"] if c["kind"] == "caption"}
for k in keys:
    src = src_by.get(k)
    ovl = ovl_by.get(k)
    if not src or not ovl:
        print(f"  missing {k}")
        continue
    # body clock = remix - intro
    body_t = ovl["start"] - intro
    src_start = float(src["start"])
    # expected body_t if perfect: remap from spans
    exp = next((r["start"] for r in remapped_probed if r["text"] == k), None)
    drift_vs_src = body_t - (src_start - spans[0][0]) if body_t < sum(probed_clips[:1] or [0]) else body_t - (src_start - spans[1][0] + (probed_clips[0] or nominal[0]))
    print(
        f"  {k[:20]}",
        f"src={src_start:.2f}",
        f"ovl={ovl['start']:.2f}",
        f"body={body_t:.2f}",
        f"exp_body={exp:.2f}" if exp is not None else "",
    )
