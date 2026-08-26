---
name: daily-discover
description: Runs YouTube/Bilibili daily discovery by topic or search query into the SQLite queue (metadata only, no audio). Use when the user asks to discover, crawl, inbox, 日更发现, 搜词, or yarn discover. For 定时/后台/告警 use ops-api. When the user also wants 主路径/验收, continue with main-path instead of stopping after enqueue.
---

# Daily discover (YouTube + Bilibili) · 旁路

> 产品主线：`trans/工具需求.md`。本 skill 只做发现入队，不是工作台工作流。

## Rules

- Platforms（**发现层**）：**youtube + bilibili only**。发布平台见 `trans/平台范围.md` §发布。
- Discovery writes **URL + metadata** to `subtitle_pipeline/data/queue.db` (`original_url` + `canonical_url`). Do **not** download WAV here.
- One MVP topic: `home_tips`（居家小技巧）. `daily: false` so 08:00 cron does **not** crawl. Manual only: `yarn discover --topic home_tips`. Spoken tutorials ≥3 min, top 5 by views. Do not add other domains unless the user asks.
- Audio is `yarn batch` / WF-01 later. See `batch-local-first`. 验收: `main-path`. 日更挂起: `ops-api`.
- Config: `subtitle_pipeline/discover/topics.yaml`.
- Docs: `trans/日更爬取需求对齐.md`, `trans/发现层设计.md`.

## Commands (cwd = subtitle_pipeline)

```bash
# MVP 居家小技巧（定时不开）
yarn discover --topic home_tips --dry-run
yarn discover --topic home_tips

# Manual URL
yarn inbox --url "https://www.youtube.com/watch?v=ID" --topic home_tips

# Offline logic
yarn discover:mock
```

YouTube metadata search may need the same local proxy as `fetch_media.py` (**socks5 10808**, not HTTP 10809).

If the user asked only for a one-off discover/inbox CLI, **stop after enqueue**. If they asked for 主路径 / 验收, follow `main-path`. If they asked for 日更 / 定时 / 后台 / 告警, follow `ops-api` (do not stop at yarn discover).

## After a run

- Report: `subtitle_pipeline/downloads/reports/discover/YYYY-MM-DD.json`
- Queue: pending rows with `original_url`, `canonical_url`, `author`, `duration_sec`, `views`, `query`
- Mongo `source_links` only if `ops-api` synced (after `yarn api` inbox/discover)
