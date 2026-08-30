---
name: batch-local-first
description: Processes pending queue jobs with ASR/translate/notes and optional export-site. Default LLM is local Ollama; optional hybrid/tokenhub hy3. Use when the user asks for yarn batch, 加工, 转写, 翻译, LLM profile, --stages, --requeue-failed, or export-site command details — not for full e2e smoke (use main-path).
---

# Batch process (local first) · WF-01 + WF-02

> 产品归属：`trans/工具需求.md` → **WF-01 翻译** + **WF-02 笔记**（+ 可选 export 旁路）。不覆盖增强/压缩/拼接/二创/发布。

## Rules

- Default profile: **local** (`gemma4:e2b` + `translategemma:4b` via Ollama).
- Cloud: `--llm-profile hybrid` (hy3 for polish/summary, local translate) or `tokenhub` (all hy3).
- Never commit API keys. Use env `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN`.
- Site pack: `--langs site` (**16 langs**, same as `transform/lib/locales.ts`; pack name `site`, frontend dir `transform/`). Default in `pipeline.py` / `yarn batch` / `yarn full`. `yarn translate` still passes `--langs all` when you want the full set.
- Notes (WF-02): source-lang `one_liner`/`summary`/`focuses`/`key_points`/`hard_points`, then localize to the same lang pack as captions.
- Parallel translate / notes localize: `--lang-workers N` or env `VITUAL_LANG_WORKERS` (default **2**, max 8). Whisper/fetch stay serial.
- After source SRT: **source notes ∥ subtitle translate** (fork). Notes localize still waits on source notes.
- GIF/MP4 cuts: parallel ffmpeg via `VITUAL_CLIP_WORKERS` (default **2**).
- Controllable `--stages`:
  - `all` — full path
  - `llm` — translate+notes+localize, **reuse SRT** (no Whisper)
  - `post` — llm + frames/clips
  - `media` / `clips` / `frames` — re-cut only (needs existing notes)
  - comma list e.g. `translate,localize`
- Whisper stays local. Secrets: `llm.yaml` / `.env` are gitignored.
- Source language in content.db / filenames must be a real code (`en`, `zh`, …), **never** `src`.
- Config sample: `subtitle_pipeline/llm.example.yaml`.
- Docs: `trans/加工闭环.md`, `trans/多语言与笔记.md`, `trans/模型可替换方案.md`. 验收: `main-path`. 日更: `ops-api`.

## Commands (cwd = subtitle_pipeline)

```bash
yarn batch                         # local, langs=site, limit=1
yarn batch:fast                    # langs=site, no-multipass, cpu
yarn batch:fast --requeue-failed   # after a failed fetch/ASR
python -m discover.run_batch --llm-profile hybrid --langs site --limit 1
yarn batch:cloud                   # tokenhub hy3
yarn export-site                   # content.db → transform/content/articles.json (includes cues)
```

Register existing work dir without ASR:

```bash
python -m discover.run_batch --register path\to\workdir --platform youtube --video-id ID --topic inbox --source-lang en
```

## Fetch / proxy

Audio download happens **here**, not in discover. For YouTube, `fetch_media.py` must prefer `socks5://127.0.0.1:10808` over env `HTTP_PROXY=http://127.0.0.1:10809`. If 403: reuse `media/full_16k.wav` in the job work dir if present, then `--requeue-failed`.

Job work dir:

```
downloads/batch/{platform}_{id}/
  media/   wav, source.*, fetch_meta, sync_meta
  subs/    {lang}.srt
  notes/   {lang}/summary.md + summary.json
```

## After a run

- Job status: pending → processing → done | failed | dead
- Content: `subtitle_pipeline/data/content.db`
- Export: `articles.json` must contain `cues` (parsed SRT), not only `srt_path`
- 前台: `cd ../transform && npm run build` (or `npm run dev`)
- Mongo logs/alerts: only if the job was started via `ops-api` (`POST /admin/batch`), not plain yarn
