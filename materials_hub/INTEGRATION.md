# 素材中心 × subtitle_pipeline 联动方案

## 1. 结论(可行性)
**可以联动,且收益明确。** subtitle_pipeline 是完整的视频处理流水线
(fetch 下载 → ASR 字幕 → 翻译 → 摘要/关键点 → 去字幕 → 后处理 → 导出),
它的产物天然就是"素材",分布在固定目录,且每个任务带有结构化元数据。
素材中心(full 离线、零依赖)可以**不复制文件**地把这些素材统一编目、检索、预览。

关键约束:**视频体量大**(downloads 下 177 个 mp4),绝不能复制到素材中心仓库。
因此联动采用 **「原路径引用」模式**(只登记索引 + 元数据,文件留在 subtitle_pipeline)。

## 2. subtitle_pipeline 的素材落点(已勘察)
```
subtitle_pipeline/
├─ downloads/
│  ├─ batch/<平台>_<ID>/          # 每个任务一个目录
│  │   ├─ media/                  # 源视频/音频(source.m4a, full_16k.wav)
│  │   │   ├─ fetch_meta.json     # 标题/平台/UP主/时长/ID ★元数据
│  │   │   ├─ glossary.json
│  │   ├─ subs/                   # 多语言字幕 *.srt
│  │   └─ notes/                  # 多语言笔记/摘要 *.md + *.json
│  ├─ benchmarks/                 # 基准测试视频 *.mp4 + *.json
│  └─ mode-renders/               # 模式渲染预览 *.jpg/*.mp4 + *.json + *.log
├─ instances/<name>/              # 单任务处理实例(如 demosaic_test 对比图)
└─ data/                          # 工作库(*.db/*.wt/*.log,跳过)
```
`fetch_meta.json` 字段示例:
`{url, platform, bvid, id, title, uploader, duration, extractor, source_file, wav}`
→ 足以生成富标签(标题/平台/任务ID/类型)。

## 3. 素材类型 → 素材中心分类映射
| subtitle_pipeline 产物 | 分类(kind) | type / role 标签 |
|---|---|---|
| `media/*.mp4` 等（有音轨，**母版**） | videos | type:media + **role:master** + role:picture + has_audio:1 |
| `media/clips/range_*.mp4`（**物理交付切片**） | videos | type:clip + **role:clip** + parent:`<母版id>` + t_start:/t_end: |
| **库内拆条**：有声视频 demux 出的无声画面 | **silent（无声）** | demux + role:silent-picture + has_audio:0 + parent: |
| `media/*.mp4` 等（本身无 audio stream） | **silent** | 直接 `classify` / `reclassify` |
| 拆出的音轨 `*_track.wav` | audio | role:audio-stem + parent: |
| `media/full_16k.wav` | audio | 同上 + **asr**（Whisper 派生，16 kHz） |

对库内已有有声视频一键拆无声：`POST /api/split-silent`（或 Python `core.split_all_videos_to_silent()`）。
原 `videos` 条目保留；新增 `silent` + `audio`。

### 3.1 四层模型（方案 A，对齐 DAM 最佳实践）

| 层 | 含义 | 本项目落点 |
|----|------|------------|
| **Master** | 权威源片，一份实体 | `role:master`；Agent 默认检索面 |
| **Logical shot** | 母版时间轴片段 | `index/shots/<id>.json` + MCP `get_shots`；**不**每镜落盘 |
| **Component** | 声画拆层 | silent / audio + `parent:` |
| **Physical clip** | 已导出供二创的文件 | `role:clip`（可选）；bridge 登记后 `link_clip_parents` |

**原则**：切片优先元数据；只有要发给下游的 mp4 才登记为 clip。回填：`python cli.py facets --link-parents`。

| `subs/*.srt` | **subs(新增)** | type:subs |
| `notes/*.md` | docs | type:notes |
| `notes/*.json` / `*.json` 元数据 | docs | type:notes / type:meta |
| `mode-renders/*.jpg/*.png` | images | type:render |
| `benchmarks/*.mp4` | videos | type:benchmark |
| `instances/*` 对比图 | images | type:test |

自动标签统一为:`sp | <platform> | job:<id> | type:<...> | lang:<xx>`(+ role/parent 面),
描述填 `title`(来自 fetch_meta.json)。

## 4. 架构(已落地)
- **core.py** 新增:
  - `subs` 分类(srt/ass/vtt/ssa/sub/sbv)。
  - 索引表加 `location` / `external_path` 两列(兼容旧库,ALTER 补列)。
  - `ingest_external(src, source, tags, description)`:**不移动/复制**,只按原路径登记,
    sha256 去重;预览时由 `external_path` 读取。
  - `remove_material` 对外部引用只删索引、不动原文件。
- **server.py**:`/api/file/<id>` 对 `location=external` 改读 `external_path`;新增 subs 预览类型。
- **bridge_subtitle.py**(新增):只读扫描 subtitle_pipeline 的 4 个 scope,
  自动归类 + 打标签 + 登记。支持 `--dry-run` / `--scope` / `--root` / `--limit`。

### 数据流
```
subtitle_pipeline 产出文件
        │  (bridge_subtitle.py 只读扫描,不搬文件)
        ▼
素材中心 index/hub.db  ──┐  location='external'
   (id, kind, tags,         │  external_path=原绝对路径
    description, sha256)    │
        │                   │
        ▼                   │
Web 面板 /api/file/<id> ───┘ 按需读 external_path 预览(图片直接显示,视频流式)
```

## 5. 触发方式(任选)
1. **流水线自动**(已落地):`discover/run_batch.py` 在 `mark_done` 后调用
   `discover/hub_push.push_work_dir_to_hub` → `bridge.run_job_dir`(只登记本任务目录)。
   关同步:环境变量 `VITUAL_HUB_SYNC=0`。
   **登记后默认**：① 有声 videos → silent+track（`VITUAL_HUB_SPLIT_SILENT=0` 关）
   ② auto 链 thumb→OCR→shots→pHash（`VITUAL_HUB_AUTOPROC=0` 关）。
2. **手动**:`python bridge_subtitle.py`(先 `--dry-run` 看统计)。
3. **轮询守护**:`python bridge_subtitle.py --watch` 每 30s
   增量扫描并登记新素材(`--interval` 调间隔、`--scope` 限定范围),Ctrl+C 退出。
4. **定时**:Windows 任务计划程序 / cron,在 batch 跑完后执行,增量登记。
   **幂等**:同 `sha256` 去重;同 `external_path` 内容变更时 **update** sha/size(不叠重复条目)。
5. **MCP(进阶)**:subtitle_pipeline 已有 `vitual_mcp`;可加一个工具把产物 POST 到
   素材中心。阶段二再做。

### 5.0 事件驱动后处理(2026-09-29 对齐 §18/§19)

入库成功后自动补齐派生数据,不必手跑 CLI:

| 入口 | 触发 |
|------|------|
| Hub `POST /api/upload` `/api/ingest` `/api/scan` | `enqueue_autoproc` 后台线程 |
| Hub `POST /api/split-silent` | 拆条成功 → 同上 |
| Hub `POST /api/auto` / `GET /api/auto/status` | 手动全量 / 查进度 |
| bridge `run_job_dir` | ① `link_clip_parents` ② `split_new_videos`(跳过 role:clip) ③ `do_auto` |
| `bridge_subtitle.py --auto` | 登记后显式跑 auto 链 |
| MCP `auto_process` / `get_shots` / `list_missing_covers` / `job_checkup` / `near_duplicate_report` | 写需 `confirm=true`;读镜头用 tool 或 `hub://shots/{id}` |
| MCP Prompts | `fill_missing_thumbs`、`job_checkup`、`segment_first`（`prompts/list` / `prompts/get`） |
| CLI `near-dupes` | 全库近重复对+簇报告 |
| CLI / MCP `imgsearch` / `search_by_image` | 以图搜图(dHash/CLIP)；`imgsearch --text` 以文搜图 |
| CLI `imgembed` / MCP `build_image_embeddings` | CLIP 图像向量索引（需 `models/clip/ViT-B-32.pt`） |
| CLI `facets [--link-parents]` | 补 role:master/clip 面标签；回填 clip+组件 `parent:` |

镜头索引:`python cli.py shots <id>` / `shots --all` → `index/shots/<id>.json`。
近重复:`python cli.py near-dupes [--max-dist 10]`。
以图/以文搜:`python cli.py phash --all`；`imgembed`；`imgsearch <id>` / `imgsearch --text "厨房"`。
Agent 技能 Cursor skill: `.cursor/skills/materials-hub-agent/SKILL.md`。

## 5.1 ASR→字幕校准链(与 PIPELINE_BEST_PRACTICES 对齐)
详见 [`subtitle_pipeline/PIPELINE_BEST_PRACTICES.md`](../subtitle_pipeline/PIPELINE_BEST_PRACTICES.md)。
摘要:16k wav → Whisper multipass → LLM-1 可疑校对 → glossary+LLM-2 全量 → 翻译 `.srt` → hub 登记。

## 6. 引用完整性(引用的固有代价,已一并处理)
只登记索引就意味着**索引可能指向已经不存在的文件**(subtitle_pipeline 清理 `mode-renders/`、
重跑 batch 覆盖旧目录时必然发生)。处理策略:

| 场景 | 行为 |
|---|---|
| 巡检 | `core.broken_externals()` / `GET /api/broken` / `bridge_subtitle.py --prune`(先 dry-run 看) |
| 感知 | 状态栏红字「失效 N」+ 失效素材卡片标红「原文件缺失」+ `GET /api/health` 的 `ok:false` |
| 清理 | `POST /api/prune` / `--prune`:**只删索引记录,绝不删除或修改磁盘文件** |
| 恢复 | 原文件回来后重跑 `bridge_subtitle.py`,sha256 幂等,自动重新登记 |

**一轮完整同步**(推荐挂后台或任务计划):
```bash
python bridge_subtitle.py --auto --embed --prune   # 登记 + 自动处理链 + 语义索引 + 引用巡检
python bridge_subtitle.py --state                    # 查看上次同步时间/新增/引用完整性
```
`--auto` 覆盖单独 `--thumbs`(auto 链已含封面);`--embed` 让新登记立刻可被自然语言搜到
(需要本机 ollama embedding 模型;没有会自动跳过)。
同步结果写入 `index/bridge_state.json`。

## 7. 取舍与后续
- **引用 vs 复制**:默认引用(省盘、幂等、不动原工程);若需素材中心自带备份,可加 `--copy` 走 `ingest_file`。
- **大视频预览**:已支持 `/api/file` HTTP Range 流式(可拖拽/边下边播),视频封面走 `/api/thumb/<id>`(ffmpeg 自动发现,无 ffmpeg 时前端 canvas 截帧兜底)。
- **删除语义**:素材中心删条目只删索引;真正清理仍回 subtitle_pipeline 操作。
- **真语义检索**:当前关键词/加权近似;接本地 Embedding 后,外部素材同样可语义搜。

## 8. 验证
- `python bridge_subtitle.py --dry-run` 统计各 scope 数量与分类。
- `python bridge_subtitle.py --limit 20` 试登记 20 个,`python cli.py list` 查看标签。
- 启动 `python server.py`,面板按 `type:benchmark` / `job:BV1aDb56iEvu` 筛选并预览。
- **实例闭环(已实测)**:把某个真实产物临时改名 → `/api/broken` 与 `/api/health` 立即反映 →
  `POST /api/prune` 清理 → 恢复文件 → 重跑 bridge 重新登记,全程索引条数回到 344、`ok:true`。
