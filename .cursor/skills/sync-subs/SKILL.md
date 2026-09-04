---
name: sync-subs
description: Keeps subtitle timelines aligned with the source video clock. Use when the user mentions SRT sync, 字幕同步, slice offset, shift-srt, or mismatched captions.
---

# Subtitle sync

> 服务 `trans/工具需求.md`：**WF-01 翻译**、**WF-03 切片**、**WF-07 二创叠层**。文档：`trans/字幕同步保障.md`。

## Charter

**声音和字幕必须对齐。** Cue times share the **audio clock of the file being played**. Do not invent a second timeline.

- Translate copies source `start/end`.
- Overlay intro = leading silence of `remix.mp4` (when body speech starts), not `_intro.mp4` container duration.
- Site player schedules HTML overlay from WebVTT `TextTrack` (same clock as audio). Do not use video-frame `mediaTime` for captions.
- When remix body is `_clips.mp4` / concatenated ranges, caption clock must stay `clips` even after remux to `_src_av.mp4` (pass `clock=` into `caption_events_for_body`).

## Musts

- Full-video embed + clip-only ASR requires `--slice-start-sec`.
- Translate write: `assert_timeline_aligned` (hard fail).
- Polish/correct must not split/merge cues.
- Embed ads offset: `yarn shift-srt --in a.srt --ms 200`.
- ASR writes `{stem}_sync_meta.json`.
- Remix overlay writes `remix.vtt` on the remix audio clock (`audio_clock: true`).

**Main path** (`main-path` / `yarn batch`): full-length embed, so `slice_start_sec=0`. Do not concatenate clip SRTs that all start at 0 onto a full-length player.

Docs: `trans/字幕同步保障.md`, `trans/字幕同步检查报告.md`. Code: `subtitle_pipeline/sync_utils.py`, `media_ops.overlay_intro_sec`.

## Commands

```bash
python pipeline.py transcribe clip.wav --slice-start-sec 42 --reuse-audio
python pipeline.py shift-srt --in a.srt --ms 200 --reason embed_ads
yarn shift-srt --in path\to\a.srt --ms -500
```
