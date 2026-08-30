---
name: ops-api
description: Starts FastAPI (:8800), admin UI, and workbench Try (/api/try/*). Use when the user asks for 后台, 管理系统, 告警, 日志, 定时, yarn api, mongod, FastAPI, admin, 工作台, 试一试, try, 贴链接, or to persist original URLs into Mongo. For yarn-only smoke/验收 use main-path.
---

# Ops API (FastAPI + Mongo + admin + Try)

> **旁路**：日更 / 定时 / 日志告警 / 工作台 Try。产品主线见 `trans/工具需求.md`。  
> Smoke/验收仍用 `main-path`（yarn）。不要另造一套 HTTP 验收主路径。

Stage manuals: `daily-discover`, `batch-local-first`, `sync-subs`. Docs: `trans/技术栈.md`, `admin/README.md`.

## Which door

| User said | Do |
|-----------|----|
| 主路径 / 验收 / 跑一遍 | `main-path` (yarn). Do not require Mongo. |
| 日更 / 定时 / 后台 / 告警 / 日志 / 启动 API | **This skill.** mongod → `yarn api` → admin `:3001` |
| 工作台 / 试一试 / try / 贴链接 / 上传视频体验 | **This skill** — `/api/try/*`（前台 UI 在 `transform/` :3000，agent 直接调 HTTP） |
| 只发现 / 只 inbox（一次性 CLI） | `daily-discover` yarn commands |

Two databases: SQLite `queue.db` / `content.db` still drive ASR. Mongo `vitual` holds `source_links` (original_url + canonical_url), `logs`, `alerts`, `runs`. Yarn-only runs do **not** write Mongo logs; ops runs do.

**Try vs batch:** `/api/try/*` is interactive (single/batch URL or upload, poll by job_id). `POST /admin/batch` is scheduled queue processing — same pipeline, different entry.

## N0 — local Mongo

```powershell
# Dedicated local mongod (port 27018). Do not use the auth-required 27017 instance on this PC.
# mongod.exe is typically: C:\Program Files\MongoDB\Server\8.3\bin\mongod.exe
```

If `yarn api` dies with "MongoDB not reachable", the API still starts with in-memory store for **Try only**. Admin persistence needs mongod. Optional env: copy `subtitle_pipeline/api/env.example` → `subtitle_pipeline/.env`.

Default token: `local-admin` (`VITUAL_ADMIN_TOKEN`). Schedule: 08:00 discover, 08:30 batch (Asia/Shanghai), only while the API process is up **and** Mongo is up.

## Start (dev vs PM2)

Dev (foreground, `--reload`):

```powershell
cd D:\MineWeb\2026\Vitual\subtitle_pipeline
yarn api
# http://127.0.0.1:8800/health  → frontend: transform/
```

Keep-alive with **PM2** (no `--reload`, **one** API process — APScheduler + job lock cannot cluster):

```powershell
cd D:\MineWeb\2026\Vitual
$env:Path = "C:\Users\msi\AppData\Local\npm-global;" + $env:Path
# mongod must already be listening on 27018
pm2 start ecosystem.config.cjs
pm2 status
pm2 logs vitual-api
```

`ecosystem.config.cjs` starts **only** `vitual-api` (:8800, one fork). Reuse the existing mongod on :27018; do not start a second Mongo. Admin UI is still `cd admin && npm run dev` on :3001. Do not use `pm2 start -i max` on the API.

`GET /health` needs no token. All `/admin/*` need header `X-Admin-Token: local-admin`.

## Workbench Try (`/api/try/*`)

Agent-facing surface — same contract as `transform/components/TryForm.tsx`. Intent/stages logic lives in `subtitle_pipeline/api/try_service.py` (source of truth; do not re-derive from TS).

1. Start API (`yarn api`). Mongo optional for Try.
2. Optional probe cookies/platforms: `POST /api/try/probe`
3. Submit: `POST /api/try/urls` (batch URLs) or `POST /api/try/uploads` (multipart)
4. Poll: `GET /api/try/{job_id}` until `status` is `done` or `failed`
5. Result `path` is like `/topics/{topic}/{slug}` — open in `transform` dev or run `yarn export-site` + build for static feed

```powershell
$base = "http://127.0.0.1:8800"

# Probe (Douyin etc. may need sessionid)
Invoke-RestMethod -Method POST -Uri "$base/api/try/probe" -ContentType "application/json" `
  -Body '{"urls":["https://www.youtube.com/watch?v=kV7RuutRx-s"]}'

# Submit URL batch (langs=site → 16 locales)
Invoke-RestMethod -Method POST -Uri "$base/api/try/urls" -ContentType "application/json" `
  -Body '{"urls":["https://www.youtube.com/watch?v=kV7RuutRx-s"],"topic":"general","langs":"site","want_translate":true,"want_notes":true}'

# Poll (replace JOB_ID)
Invoke-RestMethod -Uri "$base/api/try/JOB_ID"
```

Single-URL legacy: `POST /api/try/url`. Upload: `POST /api/try/upload` or batch `POST /api/try/uploads`. Duration probe: `GET /api/try/duration?url=...`.

After Try completes, frames/clips land under `transform/public/{frames,clips}/`. Notes feed JSON: `yarn export-site` → `transform/content/articles.json`.

MCP：`yarn mcp:smoke` 验收 API+export；tools `vitual_*` + resources `vitual://export/*`（见 `trans/MCP工具清单.md`）。

## Trigger admin jobs (cwd does not matter; API does)

```powershell
$h = @{ "X-Admin-Token" = "local-admin"; "Content-Type" = "application/json" }

Invoke-RestMethod -Method POST -Uri http://127.0.0.1:8800/admin/inbox -Headers $h -Body '{"url":"https://youtu.be/kV7RuutRx-s","topic":"ai_monetize"}'

Invoke-RestMethod -Method POST -Uri http://127.0.0.1:8800/admin/discover -Headers $h -Body '{}'
Invoke-RestMethod -Method POST -Uri http://127.0.0.1:8800/admin/batch -Headers $h -Body '{"fast":true,"limit":1}'
Invoke-RestMethod -Method POST -Uri http://127.0.0.1:8800/admin/export -Headers $h -Body '{}'

Invoke-RestMethod -Uri http://127.0.0.1:8800/admin/links -Headers $h
Invoke-RestMethod -Uri http://127.0.0.1:8800/admin/logs -Headers $h
Invoke-RestMethod -Uri http://127.0.0.1:8800/admin/alerts -Headers $h
```

POSTs return `accepted` and run in a background thread (one lock: no overlapping batch). After inbox/discover, SQLite jobs sync into Mongo `source_links`. Failures write `alerts` (optional webhook `VITUAL_ALERT_WEBHOOK_URL`) — ack via `POST /admin/alerts/{id}/ack`.

## After a run

- `/health` → `role: backend-ops`, `frontend: transform/`
- Admin UI shows 原始链接 / 日志 / 告警 (when Mongo up)
- Public SEO: `yarn export-site` then `cd ../transform && npm run build` (or `npm run dev`)
- Do not mark ops healthy if admin token 401s; Mongo down is OK for Try-only smoke
