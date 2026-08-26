---
name: ops-api
description: Starts and operates the FastAPI ops backend (local Mongo, APScheduler, admin UI). Use when the user asks for 后台, 管理系统, 告警, 日志, 定时, yarn api, mongod, FastAPI, admin, or to persist original URLs into Mongo.
---

# Ops API (FastAPI + Mongo + admin)

> **旁路**：日更 / 定时 / 日志告警。产品主线见 `trans/工具需求.md`（独立工作流）。  
> Smoke/验收仍用 `main-path`（yarn）。不要另造一套 HTTP 验收主路径。

Stage manuals: `daily-discover`, `batch-local-first`, `sync-subs`. Docs: `trans/技术栈.md`, `admin/README.md`.

## Which door

| User said | Do |
|-----------|----|
| 主路径 / 验收 / 跑一遍 | `main-path` (yarn). Do not require Mongo. |
| 日更 / 定时 / 后台 / 告警 / 日志 / 启动 API | **This skill.** mongod → `yarn api` → admin `:3001` |
| 只发现 / 只 inbox（一次性 CLI） | `daily-discover` yarn commands |

Two databases: SQLite `queue.db` / `content.db` still drive ASR. Mongo `vitual` holds `source_links` (original_url + canonical_url), `logs`, `alerts`, `runs`. Yarn-only runs do **not** write Mongo logs; ops runs do.

## N0 — local Mongo

```powershell
# Dedicated local mongod (port 27018). Do not use the auth-required 27017 instance on this PC.
# mongod.exe is typically: C:\Program Files\MongoDB\Server\8.3\bin\mongod.exe
```

If `yarn api` dies with "MongoDB not reachable", start mongod first. Optional env: copy `subtitle_pipeline/api/env.example` → `subtitle_pipeline/.env`.

Default token: `local-admin` (`VITUAL_ADMIN_TOKEN`). Schedule: 08:00 discover, 08:30 batch (Asia/Shanghai), only while the API process is up.

## Start (dev vs PM2)

Dev (foreground, `--reload`):

```powershell
cd D:\MineWeb\2026\Vitual\subtitle_pipeline
yarn api
```

Keep-alive with **PM2** (no `--reload`, **one** API process — APScheduler + job lock cannot cluster):

```powershell
cd D:\MineWeb\2026\Vitual
$env:Path = "C:\Users\msi\AppData\Local\npm-global;" + $env:Path
# mongod must already be listening on 27018
pm2 start ecosystem.config.cjs
pm2 status
pm2 logs vitual-api
# http://127.0.0.1:8800/health
```

`ecosystem.config.cjs` starts **only** `vitual-api` (:8800, one fork). Reuse the existing mongod on :27018; do not start a second Mongo. Admin UI is still `cd admin && npm run dev` on :3001. Do not use `pm2 start -i max` on the API.

`GET /health` needs no token. All `/admin/*` need header `X-Admin-Token: local-admin`.

## Trigger jobs (cwd does not matter; API does)

```powershell
$h = @{ "X-Admin-Token" = "local-admin"; "Content-Type" = "application/json" }

# original URL is stored as original_url
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

- `/health` → `mongo: true`, role `backend-ops`
- Admin UI shows 原始链接 / 日志 / 告警
- Public SEO still `cd ../site && npm run build` after export (or tell the user to refresh `site` dev)
- Do not mark ops healthy if mongod is down or admin token 401s
