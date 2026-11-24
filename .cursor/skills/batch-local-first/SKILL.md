---
name: batch-local-first
description: Processes pending queue jobs with ASR/translate/notes and optional export-site. Default LLM is local Ollama; optional hybrid/tokenhub hy3. Use when the user asks for yarn batch, 加工, 转写, 翻译, LLM profile, --stages, --requeue-failed, or export-site command details — not for full e2e smoke (use main-path).
---

# Batch process (local first) · WF-01 + WF-02

> 产品归属：`trans/工具需求.md` → **WF-01 翻译** + **WF-02 笔记**（+ 可选 export 旁路）。不覆盖增强/压缩/拼接/竖屏包装/发布/AI Remake。

## Rules

- Default profile: **local** (`gemma4:e2b` + `translategemma:4b` via Ollama).
- Cloud: `--llm-profile hybrid` (hy3 for polish/summary, local translate) or `tokenhub` (all hy3).
- Never commit API keys. Use env `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN`.
- Site pack: `--langs site` (**16 langs**, same as `transform/lib/locales.ts`; pack name `site`, frontend dir `transform/`). Default in `pipeline.py` / `yarn batch` / `yarn full`. `yarn translate` still passes `--langs all` when you want the full set.
- Notes (WF-02): source-lang `one_liner`/`summary`/`focuses`/`key_points`/`hard_points`, then localize to the same lang pack as captions.
- Parallel translate / notes localize: `--lang-workers N` or env `VITUAL_LANG_WORKERS` (default **2**, max 8). Whisper/fetch stay serial.
- **硬烧字幕 = 源文基准（有硬字幕时强制）**：
  - 画面有烧录字幕 → `hardsub_extract` OCR 直提源语 SRT，**跳过 Whisper**（`VITUAL_HARDSUB_PREFER=1` 默认开）。
  - 入库前 `hardsub_compare` 对账：源 SRT vs 原片 OCR（报告 `media/hardsub_compare.json`）。
  - 检测到硬字幕却仍是 ASR 稿 → 门禁自动升 `strict`（`VITUAL_HARDSUB_GATE`，默认 `warn` 会升）。
  - 无硬字幕才走 ASR multipass。翻译/笔记一律以硬烧对过的源 SRT 为真源，勿用 ASR 错名覆盖。
  - 手工纠源后重翻：`pipeline.py translate --from-srt <work>/subs/{src}.srt --source-lang … --force`。
  - 代码：`hardsub_extract.py` / `hardsub_compare.py` / `discover/run_batch.py`（`_try_hardsub_source` / `_hardsub_gate`）。
- ASR default（仅无硬字幕）：**multipass** + LLM-1 suspicious polish + LLM-2 glossary/full correct (see `PIPELINE_BEST_PRACTICES.md`).
- After source SRT: **source notes 与 subtitle translate 串行**（先 translate 再 notes；CPU-only Ollama 下并行会互相抢占致 summary 600s 超时，串行更快）。Notes localize 仍等待 source notes 完成。
- GIF/MP4 cuts: parallel ffmpeg via `VITUAL_CLIP_WORKERS` (default **2**).
- Controllable `--stages`:
  - `all` / `content` — 内容轨（fetch…clips），**不含** postproc
  - `llm` — translate+notes+localize, **reuse SRT** (no Whisper)
  - `post` — llm + frames/clips
  - `postproc` — 媒体轨：dehardsub,deblur,enhance,compress（第二轨，opt-in）
  - `media` / `clips` / `frames` — re-cut only (needs existing notes)
  - comma list e.g. `content,postproc` / `translate,localize,dehardsub`
- Parallel already: `--lang-workers`（字幕翻译 / 笔记 localize）；`VITUAL_CLIP_WORKERS`。**不是** notes∥translate（batch 已串行；设计见 `trans/并行化设计.md`）
- Yarn tracks: `batch:content` / `batch:fast` / `batch:postproc` / `batch:dehardsub`
- Whisper stays local. Secrets: `llm.yaml` / `.env` are gitignored.
- Source language in content.db / filenames must be a real code (`en`, `zh`, …), **never** `src`.
- Config sample: `subtitle_pipeline/llm.example.yaml`.
- Docs: `PIPELINE_BEST_PRACTICES.md`（全链路对齐）, `trans/加工闭环.md`, `trans/多语言与笔记.md`. 验收: `main-path`. 日更: `ops-api`.
## Commands (cwd = subtitle_pipeline)

```bash
yarn batch                         # local, langs=site, limit=1 (multipass on)
yarn batch:fast                    # smoke: langs=site, no-multipass, cpu
yarn batch:release                 # publish-quality: multipass ASR, langs=site, cpu
yarn batch:fast --requeue-failed   # after a failed fetch/ASR
python -m discover.run_batch --llm-profile hybrid --langs site --limit 1
yarn batch:cloud                   # tokenhub hy3
yarn export-site                   # content.db → articles.json (cues=source clock; remix=overlay)
yarn sync:audit                    # skip OK if smoke work_dir absent
yarn sync:audit:remix              # strict + require remix artifacts
yarn sync:spot                     # start/mid/end cue bounds + energy
yarn sync:gate                     # unit subset + audit + spot
yarn sync:gate:ci                  # CI: units required; audit soft-skip
```

**Profiles**

| Script | Multipass | Use |
|--------|-----------|-----|
| `batch:fast` | off | N0–N8 smoke / 验收速度 |
| `batch` / `batch:release` | on | 正式发布前压 S4/S5 口型误差 |

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
