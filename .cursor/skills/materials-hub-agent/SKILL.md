---
name: materials-hub-agent
description: Materials hub Agent skills — missing covers, job checkup, near-dupes, image search, event-driven autoproc, master/shots/clip taxonomy. Use when the user asks about 素材中心 Agent、无封面、silent 补封面、job 体检、以图搜图、近重复、hub://job、MCP prompts, 原片切片分类, 治理合规, 分发就绪, Agent 学习闭环, or materials_hub maintain.
---

# Materials Hub Agent skills

Docs: `materials_hub/素材中心最佳实践与优化分析.md` §18–§22、`materials_hub/INTEGRATION.md` §3 / §5.0。

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

## Skill F — 治理 / 合规盘点

1. MCP `governance_report` **or** `python cli.py govern [--limit N]`。
2. 检查四类治理风险:`placeholder`(占位/临时文件名)、`missing_desc`(无描述)、`missing_role`(视频类未分类为 master/clip/组件)、`untagged`(无语义标签且无 ai_tags)。
3. 仅报告,不自动改。若用户授权,可结合 `facets --link-parents` / `autotag --rule --apply` 修复。
   CLI: `python cli.py govern`。

## Skill G — 分发渠道就绪度

1. MCP `distribution_readiness` **or** `python cli.py readiness --job <id>`。
2. 复用 `job_checkup`:`channel_ready` = 体检 ok 且 封面/镜头/clip 父链/标签 齐备;`blocking` 列出阻碍分发的项(命名空间化:`missing_thumbs:`/`clips_missing_parent:`/...)。
3. 用于「某活动素材能不能发」「发给合作方前先体检」等场景。
   CLI: `python cli.py readiness --job <id>`。

## Skill H — Agent 反馈学习闭环

1. 每次 Agent 动作(采纳/否决)调用 MCP `agent_feedback(action=..., accepted=...)` 或 `python cli.py feedback --action X [--reject] [--note Y]`(加 `--reject` 表示否决)。
2. 汇总 `learning_summary` **or** `python cli.py learning [--limit N]` → 各动作采纳率 `rate` + 近期记录。
3. 仅追加写本地 `.agent_feedback.jsonl`,不碰资产;驱动「越用越准」。

## Skill I — 目标→素材包组装（多智能体编排）

1. 用户说「组装 / 打包 / 素材包 / 混剪包 / package」类目标时,命中命名技能 `package`(确定性工作流,不经 7B 规划器,零幻觉)。
2. 内部跑角色链:**Librarian**(目标拆解成多查询 + 检索,只收真实命中)→ **Critic**(确定性核验:真实存在 + `scope` 过滤,丢弃编造/不符 id)→ **Executor**(组装 manifest JSON 落到 `agent_workspace/packages/`,**只读资产,绝不改动**)。
3. MCP `assemble_package(goal, kind?, scope?, limit?, queries?)` 或 CLI `python cli.py package "目标" [--kind videos] [--scope master|clips|all] [--limit N]`。
4. 交付物为 `materials-hub/package@1` manifest(含 goal / queries / assets[].role / has_cover / suggested_next);物理切片/导出/补封面仍走既有 `auto` / `autotag` / 工作台。

## Skill J — 按需交付（转码 / 区间裁剪 / 格式归一）

1. 用户说「导出 / 交付 / 转码 / deliver / export」类意图时,命中命名技能 `deliver`(确定性工作流,不经 7B 规划器)。**裁剪意图优先**:任务含「裁剪/crop/剪辑」且点名素材 id 时仍走 Skill 裁剪,「裁剪并导出」不误导向 deliver。
2. 消费 `assemble_package` 产出的素材包 manifest 或指定 id 列表,导出为下游 AIGC/剪辑可用的交付变体:`core.deliver_package` 拼 ffmpeg 命令(转码 libx264 / 区间裁剪 `-ss`+`-t` / 格式归一 `-f`),落 `index/agent_workspace/deliveries/` 派生目录。
3. 安全铁律:只读原素材、只新建交付文件、绝不改动资产本体。`confirm=false`(默认)仅返回 dry-run 计划不写文件;`confirm=true` 才真正导出(MCP `deliver_package` 与 Agent 工具 `deliver` 均强制 `confirm=true`,与写护栏一致)。
4. MCP `deliver_package(manifest?, ids?, fmt?, res?, clips?, out_dir?, copy_only?, overwrite?, confirm(必须 true))` 或 CLI `python cli.py deliver --manifest <pkg.json> [--ids id1,id2] [--fmt mp4] [--res 720|1080|0] [--out-dir DIR] [--copy-only] [--overwrite] [--confirm]`(默认 dry-run)。

## Skill K — 一键出片（package → deliver 串联）

1. 用户说「出片 / onego / pipeline」，或同时含「组装|打包|混剪|package」+「导出|转码|交付|deliver|export」时,命中命名技能 `publish`(确定性串联,不经 7B 规划器)。
2. 流程:**先组装**(复用 Skill I 的 Librarian→Critic→Executor 出 manifest)**再交付**(用该 manifest 调 `core.deliver_package` 导出变体);素材包 0 条时直接止步,不进交付。
3. 闭环「素材检索→二次创作」:一句话目标直接落到可用交付文件。写操作仍需授权(`--write` / `confirm=true`),否则交付为 dry-run。
4. CLI `python cli.py publish "目标" [--confirm] [--fmt mp4] [--res 720|1080|0]`。
5. **刻意不新增 MCP 工具**:`publish` 只是 `assemble_package`+`deliver_package` 的确定性串联,宿主自行两步调用即可;新增工具会扩大 MCP 面、增加 token 与维护成本(对齐 §15.1「降 token 膨胀/窄作用域」)。

## 意图路由（先于规划）

规则在 `agent._classify_turn`，7B 规划器之前。详见 `materials_hub/素材中心最佳实践与优化分析.md` §21。

| 场景 | 做什么 |
|------|--------|
| 问候 / help / 你是谁 | 直接回复能力清单，不调工具 |
| 天气、笑话等跑题 | 说明只处理素材库 |
| 「帮我看看」「检查一下」、没有上文的「继续」 | 追问技能，禁止默认 search+maintain+job_checkup |
| 有上文的「继续」 | 开放循环 |
| 规划结果只是未请求的工具名 | 丢弃，改为追问 |
| 技能 A–K / 上传视频 | 确定性工作流，见上文 |
| 治理/合规/占位/未分类盘点 | 技能 F:`governance_report` |
| 某 job 能否发布 / 分发就绪 | 技能 G:`distribution_readiness` |
| 记录采纳/否决 / 看学习概览 | 技能 H:`agent_feedback` / `learning_summary` |
| 组装/打包/混剪/素材包 | 技能 I:`assemble_package` |
| 导出/交付/转码素材包或 id | 技能 J:`deliver_package`(需 confirm) |
| 出片/组装并导出/onego | 技能 K:`publish`(package→deliver 串联,需 confirm) |
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
