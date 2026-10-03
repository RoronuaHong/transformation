---
name: materials-hub-agent
description: Materials hub Agent skills — missing covers, job checkup, near-dupes, image search, event-driven autoproc, master/shots/clip taxonomy. Use when the user asks about 素材中心 Agent、无封面、silent 补封面、job 体检、以图搜图、近重复、hub://job、MCP prompts, 原片切片分类, or materials_hub maintain.
---

# Materials Hub Agent skills

Docs: `materials_hub/素材中心最佳实践与优化分析.md` §18–§19、`materials_hub/INTEGRATION.md` §3 / §5.0。

Write ops always need `confirm=true` (MCP) / `--write` (CLI agent). Prefer read-only first.

## Taxonomy (方案 A — 必遵)

| 层 | 是什么 | 怎么找 |
|----|--------|--------|
| **Master** | 有声源片 `kind=videos` + `role:master` | 默认检索目标 |
| **Logical shot** | `index/shots/<id>.json` 时间轴 | `get_shots` / `hub://shots/{id}` — **主路径** |
| **Component** | demux `silent` / `audio` | `role:silent-picture` / `role:audio-stem` + `parent:` |
| **Physical clip** | SP `media/clips/range_*.mp4` | `role:clip` + `parent:` + `t_start:`/`t_end:` — **仅导出时** |

不要把每个镜头落成独立入库视频；不要把 silent demux 当成「剪辑切片」。

## Skill A — 无封面盘点 / 补封面

1. MCP tool `list_missing_covers` (`kind=silent` 优先) **or** Prompt `fill_missing_thumbs`.
2. Report `id/kind/name`. Do not invent ids.
3. If user authorized writes: `auto_process(confirm=true)` or `POST /api/thumbs`. Never delete masters.

CLI: `python cli.py thumbs` / `python cli.py auto` (cwd=`materials_hub`).

## Skill B — job 全套体检

1. MCP tool `job_checkup(job_id=…)` **or** Prompt `job_checkup` with `job_id`.
2. Optionally read Resource `hub://job/{jobid}` / `hub://shots/{id}`.
3. Summarize: kinds, `master_ids` / `clip_ids` / `component_ids`, `missing_thumbs`, `missing_shots`(仅母版), `clips_missing_parent`, `asr_ids`, `broken_ids`.

Deep Agent: `agent_run` task like「体检 job upload_96e97c8ff04f」— maintain + `job_checkup`.

## Skill C — 全库近重复

1. MCP `near_duplicate_report` **or** `python cli.py near-dupes [--max-dist 10]`.
2. Read `clusters`（一实体多引用）与 `pairs`（汉明距离）。
3. Report only — do not delete without explicit user confirm. sha256 exact dupes remain `cli.py dupes`.

Deep Agent `maintain` already samples `near_dupe_clusters` / `near_dupe_pairs`.

## Skill D — 以图搜图 / 以文搜图

1. dHash（零模型）: `python cli.py phash --all` → `imgsearch <id|path>` 或 MCP `search_by_image`.
2. CLIP（需权重 `models/clip/ViT-B-32.pt`，见该目录 README）:
   - `python cli.py imgembed` 建索引
   - `python cli.py imgsearch --text "厨房"` 以文搜图
   - `python cli.py imgsearch <id> --mode clip` 以图搜图
3. 无权重时 `clip_probe`→`no_local_weights`，自动降级 phash；不要空等下载。
4. 自然语言找画面也可：OCR + `search_materials` / `chunk_search`.

## Skill E — 找片段（segment_first）

1. MCP Prompt `segment_first` **or** Deep Agent tools `get_shots` / `related`.
2. Search masters (`role:master` / `type:media`) → `get_shots`.
3. Only if user wants exported files: search `role:clip`; check `parent:` + timecodes.
4. Components (`silent`/`audio`) ≠ editorial clips.
5. CLIP：`search_by_text_image` / `search_by_image(mode=clip)`（Deep Agent 已注册）。

Backfill tags: `python cli.py facets --link-parents`（补 role:master + clip/组件 `parent:`）。

## 意图路由（先于规划）

规则在 `agent._classify_turn`，7B 规划器之前。详见 `materials_hub/素材中心最佳实践与优化分析.md` §21。

| 场景 | 做什么 |
|------|--------|
| 问候 / help / 你是谁 | 直接回复能力清单，不调工具 |
| 天气、笑话等跑题 | 说明只处理素材库 |
| 「帮我看看」「检查一下」、没有上文的「继续」 | 追问技能，禁止默认 search+maintain+job_checkup |
| 有上文的「继续」 | 开放循环 |
| 规划结果只是未请求的工具名 | 丢弃，改为追问 |
| 技能 A–E / 上传视频 | 确定性工作流，见上文 |
| 裁剪且给出 id / 上传 / 起止秒 | 逻辑时间窗。点名片头、片尾、主戏只留该段。说导出也不在这里编码 |
| 只说「裁剪」没有对象 | 追问 id 和起止秒 |
| 还有未做完的待办就结束 | 进度按已完成条数，文案「部分完成」 |

## Deep Agent 全链路 E2E

脚本：`python scripts/e2e_agent_fullchain.py [--write] [--max-steps 12]`  
默认优先 `qwen2.5:7b`（`gemma4:e2b` 易吐非法 JSON）。  
曾缺 `get_shots` 会导致 Agent 空转耗尽步数——已补进 `_build_tools`。

## Event-driven (already wired)

| Trigger | Effect | Off switch |
|---------|--------|------------|
| upload / ingest / scan / split-silent | `enqueue_autoproc` | server process |
| bridge `run_job_dir` / hub_push | link-parents → split-silent(母版) → auto | `VITUAL_HUB_SPLIT_SILENT=0`, `VITUAL_HUB_AUTOPROC=0` |
| bridge CLI | `--auto` | — |

## MCP prompts

- `prompts/list` → `fill_missing_thumbs`, `job_checkup`, `segment_first`
- `prompts/get` with `arguments`
