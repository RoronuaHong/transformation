---
name: main-path
description: Orchestrates the full inbox → batch → export-site → Next.js build path (N0–N8). Use when the user asks for 主路径, 全链路, 跑一遍, 验收, 导出站点, smoke, or to run the site pipeline end-to-end without naming a single stage.
---

# Main path (orchestrator)

> 产品总需求：`trans/工具需求.md`。  
> **本文只编排已实现验收**：WF-01 翻译 + WF-02 笔记 + 导出/SEO 旁路（N0–N8）。  
> 不是「全部工作流」总编排；增强/压缩/拼接/二创/发布另立 skill。

Stage manuals：`daily-discover`、`batch-local-first`、`sync-subs`。人类文档：`trans/全链路验证清单.md`、`trans/加工闭环.md`。

## Default vs daily

| User said | Do |
|-----------|----|
| 主路径 / 验收 / 跑一遍 / 导出站点 (no URL) | **Smoke (yarn):** inbox the 60s clip below → `yarn batch:fast` → export → **前台** `site` build |
| 日更 / 定时 / 后台 / 告警 | **`ops-api`** (mongod + `yarn api` + admin). Do not only run yarn discover here. |
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

# N2–N7 — 16-lang site pack, no multipass
yarn batch:fast --requeue-failed

# N8 — JSON must include cues[]; then Next SSG
yarn export-site
cd ../site && npm run build
```

Langs: `--langs site` = 16 codes, same set as `site/lib/locales.ts`. Priority nav order (human): 中英俄日韩葡德, then 繁中/西/法/阿/印地/印尼/越/泰/土. Code `pt` (not `pt-br`). Notes must include structured fields (see `trans/多语言与笔记.md`). Pipeline bare default is also `site`.

LLM: default **local**. Cloud only if the user asked: `--llm-profile hybrid` or `tokenhub`.

Full-video embed: `slice_start_sec=0` (see `sync-subs`). Do not invent a clip offset on this path.

Yarn smoke does not write Mongo `logs`/`alerts`. Original URL is still stored on SQLite `jobs.original_url`. Ops persistence: `ops-api`.

## N0 / N2 failure

YouTube fetch must prefer **`socks5://127.0.0.1:10808`**. Env `HTTP_PROXY=http://127.0.0.1:10809` is flaky — `fetch_media.py` should ignore it when 10808 is up. If a run still 403:

1. Confirm 10808 is listening.
2. If `downloads/batch/<platform>_<id>/full_16k.wav` exists, leave it (reuse).
3. Else copy a previously verified wav into that path as `full_16k.wav`.
4. `yarn batch:fast --requeue-failed`.

Never mark N8 done if `site/content/articles.json` has titles but `cues` still look like the two-line Phase-0 sample.

## After a run

- Queue job `done`; `content.db` article + 16 locales (source lang is a real code, **not** `src`).
- `articles.json` has per-article `cues` with `start`/`end`/`text.<locale>`.
- `npm run build` lists `/{locale}/topics/ai_monetize/<slug>` for the 16 locales.
- Report N0–N8 pass/fail in that order. Do not start a second video unless asked.
