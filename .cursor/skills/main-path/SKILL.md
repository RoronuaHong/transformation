---
name: main-path
description: Orchestrates the full inbox → batch → export-site → Next.js build path (N0–N8). Use when the user asks for 主路径, 全链路, 跑一遍, 验收, 导出站点, smoke, or to run the site pipeline end-to-end without naming a single stage. For yarn batch / LLM profile only, use batch-local-first. For 工作台/试一试/try, use ops-api.
---

# Main path (orchestrator)

> 产品总需求：`trans/工具需求.md`。  
> **本文只编排已实现验收**：WF-01 翻译 + WF-02 笔记 + 导出/SEO 旁路（N0–N8）。  
> 不是「全部工作流」总编排；增强/压缩/拼接/二创/发布另立 skill。

Stage manuals：`daily-discover`、`batch-local-first`、`sync-subs`。人类文档：`trans/全链路验证清单.md`、`trans/加工闭环.md`。

## Default vs daily

| User said | Do |
|-----------|----|
| 主路径 / 验收 / 跑一遍 / 导出站点 (no URL) | **Smoke (yarn):** inbox the 60s clip below → `yarn batch:fast` → export → **前台** `transform` build |
| 日更 / 定时 / 后台 / 告警 | **`ops-api`** (mongod + `yarn api` + admin). Do not only run yarn discover here. |
| 工作台 / 试一试 / try / 贴链接体验 | **`ops-api`**（`/api/try/*`）。不要用本 smoke 冒充 Try。 |
| Explicit `--url` for 验收 | inbox that URL, then the rest of smoke |

Do **not** stack `yarn discover:mock` with inbox. One enqueue path only. Do **not** send 验收 through FastAPI unless the user asked for 后台/日志.

Smoke URL (already verified): `https://www.youtube.com/watch?v=kV7RuutRx-s`  
Topic: `ai_monetize`

## Sequence (cwd = `subtitle_pipeline`)

```bash
# N0 — Ollama local models present
ollama list
# Expect gemma4:e2b and translategemma:4b

# N1 — enqueue (skip if user already has pending)
yarn inbox --url "https://www.youtube.com/watch?v=kV7RuutRx-s" --topic ai_monetize --title "What is Artificial Intelligence? | AI Explained in 60 Seconds"

# N2–N7 — 16-lang site pack, no multipass (smoke speed)
yarn batch:fast --requeue-failed

# Quality pass (multipass ASR) before a real publish — not required for smoke:
# yarn batch:release --requeue-failed

# N8 — JSON must include cues[]; then Next SSG
yarn export-site
cd ../transform && npm run build

# Optional sync gate (needs smoke work_dir from a prior remix run):
# yarn sync:audit:remix
# yarn sync:spot
# yarn sync:gate
```

Langs: `--langs site` = 16 codes, same set as `transform/lib/locales.ts`（pack 名 `site`，目录名 `transform`）。Priority nav order (human): 中英俄日韩葡德, then 繁中/西/法/阿/印地/印尼/越/泰/土. Code `pt` (not `pt-br`). Notes must include structured fields (see `trans/多语言与笔记.md`). Pipeline bare default is also `site`.

LLM: default **local**. Cloud only if the user asked: `--llm-profile hybrid` or `tokenhub`.

Full-video embed: `slice_start_sec=0` (see `sync-subs`). Do not invent a clip offset on this path.

Yarn smoke does not write Mongo `logs`/`alerts`. Original URL is still stored on SQLite `jobs.original_url`. Ops persistence: `ops-api`.

## N0 / N2 failure

YouTube fetch / proxy：见 `batch-local-first`（优先 socks5 `10808`）。若仍 403：复用已有 `full_16k.wav` 或拷入后 `yarn batch:fast --requeue-failed`。

Never mark N8 done if `transform/content/articles.json` has titles but `cues` still look like the two-line Phase-0 sample.

Optional MCP gate（API 已启时）：`yarn mcp:smoke` — 通过 `/health` + export index；不等于 N0–N8 全绿。

## After a run

**Content green (N0–N8)** — does **not** require remix:

- Queue job `done`; `content.db` article + 16 locales (source lang is a real code, **not** `src`).
- `articles.json` has per-article `cues` with `start`/`end`/`text.<locale>`.
- `npm run build` lists `/{locale}/topics/ai_monetize/<slug>` for the 16 locales.
- `media/media_status.json` may show `postproc=skipped` / `remix=skipped` — that is OK for content smoke.

**Remix green (optional WF-07)** — only when stages include `remix` (Try `want_remix`, or `--stages …,remix`):

- `media/remix/remix.mp4` + `remix.vtt` + `remix_cues.json` (`audio_clock: true`).
- `media_status.remix=ok` (job can still be `done` if content succeeded while remix failed — check this file).
- Canonical names only (`remix.*`); no `remix_zh` / `remix_ja` leftovers.
- `python sync_audit.py <work_dir>` → no failures.

Report N0–N8 pass/fail in that order. Say explicitly whether remix was in scope. Do not start a second video unless asked.
