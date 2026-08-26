---
name: sync-subs
description: Keeps subtitle timelines aligned with the source video clock. Use when the user mentions SRT sync, 字幕同步, slice offset, shift-srt, or mismatched captions.
---

# Subtitle sync

> 服务 `trans/工具需求.md`：**WF-01 翻译**、**WF-03 切片**（及未来 WF-07 烧录字幕）。文档：`trans/字幕同步保障.md`。

## Principle

Cue times must share the **same clock** as the video the viewer sees. Translate copies source `start/end`; do not invent a new timeline.

## Musts

- Full-video embed + clip-only ASR requires `--slice-start-sec`.
- Translate write: `assert_timeline_aligned` (hard fail).
- Polish/correct must not split/merge cues.
- Embed ads offset: `yarn shift-srt --in a.srt --ms 200`.
- ASR writes `{stem}_sync_meta.json`.

**Main path** (`main-path` / `yarn batch`): full-length embed, so `slice_start_sec=0`. Do not concatenate clip SRTs that all start at 0 onto a full-length player.

Docs: `trans/字幕同步保障.md`, `trans/字幕同步检查报告.md`. Code: `subtitle_pipeline/sync_utils.py`.

## Commands

```bash
python pipeline.py transcribe clip.wav --slice-start-sec 42 --reuse-audio
python pipeline.py shift-srt --in a.srt --ms 200 --reason embed_ads
yarn shift-srt --in path\to\a.srt --ms -500
```
