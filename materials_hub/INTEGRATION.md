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
| subtitle_pipeline 产物 | 分类(kind) | type 标签 |
|---|---|---|
| `media/*.mp4` 等 | videos | type:media |
| `media/*.m4a/*.wav` | audio | type:media |
| `subs/*.srt` | **subs(新增)** | type:subs |
| `notes/*.md` | docs | type:notes |
| `notes/*.json` / `*.json` 元数据 | docs | type:notes / type:meta |
| `mode-renders/*.jpg/*.png` | images | type:render |
| `benchmarks/*.mp4` | videos | type:benchmark |
| `instances/*` 对比图 | images | type:test |

自动标签统一为:`sp | <platform> | job:<id> | type:<...> | lang:<xx>`,
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
1. **手动**:`python bridge_subtitle.py`(先 `--dry-run` 看统计)。
2. **轮询守护(零改动 subtitle_pipeline)**:`python bridge_subtitle.py --watch` 每 30s
   增量扫描并登记新素材(`--interval` 调间隔、`--scope` 限定范围),Ctrl+C 退出。
   适合长期挂一个后台进程,流水线产出即自动入编目。
3. **定时**:Windows 任务计划程序 / cron,在 batch 跑完后执行,增量登记(sha256 去重保证幂等)。
4. **流水线内钩子(进阶,需改 subtitle_pipeline)**:在 `discover/run_batch.py` 收尾调用
   `bridge_subtitle.py --scope batch`,或在 `api/app.py` 暴露 `POST /api/register-asset`
   转发到素材中心服务。默认**不改动 subtitle_pipeline**,保持只读联动。
5. **MCP(进阶)**:subtitle_pipeline 已有 `vitual_mcp`;可加一个工具把产物 POST 到
   素材中心(素材中心未来可加 `/api/register` 接收绝对路径)。阶段二再做。

## 6. 取舍与后续
- **引用 vs 复制**:默认引用(省盘、幂等、不动原工程);若需素材中心自带备份,可加 `--copy` 走 `ingest_file`。
- **大视频预览**:已支持 `/api/file` HTTP Range 流式(可拖拽/边下边播),视频封面走 `/api/thumb/<id>`(ffmpeg 自动发现,无 ffmpeg 时前端 canvas 截帧兜底)。
- **删除语义**:素材中心删条目只删索引;真正清理仍回 subtitle_pipeline 操作。
- **真语义检索**:当前关键词/加权近似;接本地 Embedding 后,外部素材同样可语义搜。

## 7. 验证
- `python bridge_subtitle.py --dry-run` 统计各 scope 数量与分类。
- `python bridge_subtitle.py --limit 20` 试登记 20 个,`python cli.py list` 查看标签。
- 启动 `python server.py`,面板按 `type:benchmark` / `job:BV1aDb56iEvu` 筛选并预览。
