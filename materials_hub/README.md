# 素材中心 · Materials Hub

通用多媒体素材库(任意项目的图片 / 视频 / 文档 / 音频统一管理),零依赖,纯 Python 标准库 + 单页前端,**全离线可运行**。

已接入首个上游项目 **subtitle_pipeline**(视频处理流水线),以「原路径引用」方式编目其全部产物(详见 `INTEGRATION.md`)。

## 目录
- [功能](#功能对应需求四件套)
- [架构与数据模型](#架构与数据模型)
- [元数据与分类法(Taxonomy)](#元数据与分类法taxonomy)
- [目录结构](#目录结构)
- [运行](#运行)
- [Web 面板用法](#web-面板用法)
- [检索 / 分面检索](#检索示例)
- [语义检索(本地 embedding)](#语义检索本地-embedding)
- [联动 subtitle_pipeline](#联动-subtitle_pipeline快速上手)
- [运维:备份 / 重建 / 迁移](#运维备份--重建--迁移)
- [引用完整性与健康检查](#引用完整性与健康检查)
- [预览与缩略图策略](#预览与缩略图策略)
- [Python API](#python-apicore)
- [HTTP API](#http-apiserverpy)
- [已知限制与路线图](#已知限制与路线图)
- [FAQ](#faq)

## 功能(对应需求四件套)
- **目录规范**:固定结构,素材按类型归档。
- **自动采集整理**:SHA-256 去重、自动分类、命名规范化;可扫描 `ingest/` 或 `materials/` 全树,也可只读联动外部项目。
- **索引 + 检索**:SQLite 索引 + **两段式检索**——
  ① 词法层:中文按字 / bigram 分词、英数按词,字段加权打分,配**领域同义词表**(中文→语料英文词)与**长词松匹配**;
  ② 语义层:接本地 ollama embedding 做**稠密+词法混合**排序。支持按类型筛选、按标签过滤、分页。
- **管理面板(Web/UI)**:浏览器可视化浏览、上传(含拖拽)、打标签、编辑描述、预览、视频封面、删除、去重整理、导出、引用巡检。
- **引用完整性与健康检查**:外部引用指向的原文件被上游删除/移动时能**被发现**(面板红字提示 + 逐卡标注 + `/api/health`),并可一键清理失效索引(绝不触碰磁盘)。

> 另见 `INTEGRATION.md` 了解与 subtitle_pipeline 的联动设计(可行性、目录落点、标签映射、数据流)。

## 架构与数据模型

### 两种存储模式
| 模式 | 字段 `location` | 行为 | 用途 |
|---|---|---|---|
| 内部 `internal` | `internal`(默认) | 文件**移入** `materials/<kind>/`,索引记 `rel_path` | 自己的素材,真正归集 |
| 外部引用 `external` | `external` | **不移动 / 不复制**,索引记 `external_path`(原绝对路径) | 跨项目联动(如 subtitle_pipeline 的大视频),省盘、幂等 |

外部引用的素材删除时**只删索引,不动原文件**;预览时由 `/api/file/<id>` 按需读取 `external_path`。

### 索引字段
`id`(sha256 前 12 位)、`kind`、`ext`、`name`、`rel_path`、`size`、`sha256`、`tags`、`description`、`source`、`orig_name`、`created_at`、`location`、`external_path`。

### 元数据与分类法(Taxonomy)

> 成熟的素材/数字资产管理(DAM)**首要是元数据与分类法标准**:先定好"怎么打标签",检索与分面过滤才靠谱。素材中心采用一套**受控(controlled)标签体系**,请勿自由发挥。

联动 subtitle_pipeline 时自动生成的标签(受控词汇):
```
sp | <platform> | job:<id> | type:<media|clip|subs|notes|benchmark|render|test>
  | role:<master|clip|silent-picture|audio-stem|picture> | parent:<id> | t_start: | t_end: | lang:<xx>
```
- `sp` —— 来源·流水线(subtitle_pipeline 登记)
- `upload` —— 来源·本机上传
- `<platform>` —— bilibili / youtube 等(受控:只填已知平台)
- `job:<id>` —— 对应某次采集任务(同一任务的全套素材可一键筛出)
- `type:<...>` —— 业务类型；`clip`=物理交付切片(非新 kind)；未知路径**不**再写 `type:batch`
- `role:master` / `role:clip` —— 母版 vs 物理切片；逻辑镜头只在 `shots` sidecar
- `role:silent-picture` / `role:audio-stem` —— 拆条组件(≠剪辑切片)
- `parent:<id>` —— 子件连回母版；旧别名 `from:` **已弃用**(读兼容,新写入与面板均不再用)
- `lang:<xx>` —— 从文件名识别的语言(zh/en/ja…)

面板侧栏显示**人读标签**(如「角色·母版」「类型·源片」),悬停可见原始 `key:value`;过滤仍用原始值。
冗余面默认不进标签云:`from:` / `has_audio:` / `role:picture` / 时间码。

**扩展规则(保持一致,便于检索)**:
1. 维度用 `key:value` 形式(`job:`、`type:`、`lang:`、`role:`、`parent:`),便于分面过滤;自由文本放进 `description`,别塞进 tags。
2. 新增类型只扩 `type:` 的受控枚举,不要发明新前缀;新平台加到 `<platform>` 白名单。
3. 标签统一小写;多个标签用逗号分隔,顺序无关(检索按集合匹配)。
4. 手动上传的素材同样建议套用 `type:` / `lang:` 维度,保持全库一致。
5. 找片段默认 `role:master` + `get_shots`;不要把每个镜头拆成独立入库视频。

### 分类(kind)与扩展名
`images` · `videos` · `docs` · `audio` · **`subs`(srt/ass/vtt/ssa/sub/sbv)** · `other`。

### 检索打分(词法层)
字段权重:`name 3.0` > `tags 2.5` > `description 1.5` > `rel_path 1.0`。query 与各字段 token 重叠累计得分,按分排序;无 query 时按时间倒序。
另外两条提升召回的规则(都经真实语料实测):
1. **领域同义词表**:中文词映射到语料里的英文词(如「字幕」→ subs/hardsub/dehardsub)。仅「加词」不改词,原命中不会消失。
2. **长词松匹配**:英文/数字 token ≥5 字符时互为子串也算命中(权重 0.6)。必要性:数据里是 `demosaic`,查 `mosaic` 严格分词匹配不上。

## 目录结构
```
materials_hub/
├─ core.py            # 核心库:分类/去重/命名/索引/检索/外部引用
├─ cli.py             # 命令行入口
├─ server.py          # 轻量 Web 服务(零依赖 http.server)
├─ bridge_subtitle.py # subtitle_pipeline 只读联动桥接(新增)
├─ static/
│  └─ index.html      # 管理面板前端(单页)
├─ materials/         # 内部素材(整理后落位)
│  ├─ images/ videos/ docs/ audio/ subs/ other/
├─ ingest/            # 待整理队列(丢这里,点「整理」或跑 ingest)
├─ trash/             # 去重/删除移入的回收区
└─ index/
   ├─ hub.db             # SQLite 索引库(派生,可重建)
   ├─ thumbs/            # 视频封面缓存 <id>.jpg / <id>.jpg.fail(派生,可重建)
   └─ bridge_state.json  # 上次同步状态(时间 / 新增 / 引用完整性,供运维查看)
```
> 仓库默认不入库 `*.md` / `*.html`;素材中心的文档与单页前端已在 `.gitignore` 末尾加 `!materials_hub/**` 白名单,可随代码一起版本化(参照 `kb/docs`、`box-lift` 先例)。`index/hub.db` 为生成物,不入库。

## 运行
需要 Python 3.12(避免 3.13 移除的 `cgi`;本服务已手写 multipart 解析,但建议 3.12)。

```bash
cd materials_hub
python server.py        # 启动面板,打开 http://localhost:8000
```
或纯命令行(见下方 CLI 参考)。

> **ffmpeg 可选**:找得到就用它抽视频封面,找不到自动降级为浏览器截帧。探测顺序见[预览与缩略图策略](#预览与缩略图策略)。

## Web 面板用法
- **搜索框**:自然语言 / 关键词,实时(输入即搜),按相关度排序(默认混合检索,可切「精确关键词 / 纯语义」)。
- **类型下拉**:按 `kind` 筛选(图片/视频/文档/音频/其他)。
- **上传**:点「上传」或把文件**拖拽**到页面 → 落到 `ingest/` 并整理入库。
- **标签 / 描述**:每张卡片可编辑,点「保存」写回索引。
- **预览**:图片直接显示缩略图;视频内嵌播放器 + 封面;音频 / 文档显示占位。
- **删除**:删内部素材会移入 `trash/`(不破坏原工程);删外部引用只删索引。
- **整理 / 扫描**:「整理 ingest/」处理待整理队列;「扫描 materials/」补录全树。
- **分页**:页面底部「上一页 / 下一页 + 24·48·96 / 全部」;筛选或搜索变化自动回到第 1 页,并显示「第 X / Y 页 · 共 N 条」。
- **可分享 / 可收藏的视图**:筛选条件、每页大小、页码实时同步到 URL,复制地址即可还原同一视图;浏览器前进 / 后退可用。
  例:`http://localhost:8000/?kind=videos&size=24` 或 `.../#tag=type:benchmark&page=2`(`?` 与 `#` 两种写法都认,`#` 优先)。
- **状态栏**:显示总数、重复组数、各 kind 数量、封面缓存状态(服务端抽帧 / 浏览器截帧)。
- **重复清理**:点状态栏的「重复 N 组」或工具条「重复清理」,弹出按 SHA-256 相同的重复分组,可逐条删除冗余(保留需要的那份)。
- **生成封面**:工具条「生成封面」→ 后台批量抽帧全部视频封面,状态栏实时显示进度;抽不出封面的(源文件损坏)会标注「封面不可用」。
- **引用巡检**:状态栏「失效 N」红字可点,弹窗列出源文件已消失的外部引用,可「清理全部」(只删索引)。

## 检索示例
- 自然语言:`python cli.py search --file q.txt`(文件内容「去除字幕只留背景」)→ 命中 `dehardsub` 阶段的成片(纯词法在这里是 0 命中,语义补召回)。
- 中文/英文关键词都行;命令行中文受 Windows 终端编码影响时用 `--file` 或 `--stdin`(见下)。
- 标签筛选:`job:BV1aDb56iEvu`(某 B 站视频全套素材)、`type:benchmark`(基准视频)、`type:render`(渲染预览)、`lang:zh`(中文字幕/笔记)。
- 组合:在面板里先选 `类型=视频`,再搜 `render` 或 `benchmark`。
- 空 query:直接按时间倒序列出(等同浏览全部)。

### 分面检索(Faceted Search)
成熟 DAM 的检索 = **关键词 + 多个维度同时过滤**。素材中心提供三个正交维度,可任意组合:
- **类型(kind)**:`images / videos / docs / audio / subs / other`(面板下拉)
- **标签(tag)**:`type:` / `job:` / `lang:` / `sp` 等(面板标签下拉,含计数)
- **时间(created_at)**:无 query 时默认按时间倒序
> 例:选 `类型=视频` + 标签 `type:benchmark` + 搜 `render` ⇒ 只列"基准视频里和渲染相关"的素材。维度越多越精。
> 结果分页展示(默认 48/页,可切 24/96/全部);筛选或搜索变化自动回到第 1 页,上传/整理后也会回到第 1 页看新素材。

## CLI 参考
```bash
python cli.py init                 # 初始化目录结构 + 数据库(幂等)
python cli.py ingest [path]        # 整理 path(默认 ingest/)下文件
python cli.py scan                 # 扫描 materials/ 全树补录索引
python cli.py search <关键词>      # 加权语义近似检索
python cli.py dupes                # 列出重复文件(SHA-256 相同)
python cli.py list                 # 列出全部素材
python cli.py thumbs               # 批量生成视频封面(逐条打印进度;无 ffmpeg 会提示)
python cli.py thumbs --purge       # 清空封面缓存与失败标记
python cli.py embed                # 增量构建语义检索索引
python cli.py embed --force        # 全量重建;--status 只看状态
python cli.py search --file q.txt  # 中文查询建议走文件(Windows 终端 GBK 代码页会弄坏命令行中文)
python cli.py search --stdin       # 或从标准输入读查询
python eval_search.py --baseline  # 检索质量评估(16 查询人工标注 ground truth,P@5/R@20/MRR/NDCG@10,lexical↔auto 对比,回归门禁)
python cli.py ocr <id> [--force]  # 视频画面 OCR(离线 rapidocr,文本入检索;--all 批量)
python cli.py facets [--limit N] [--link-clips|--link-parents] [--scrub]   # 补 DAM 面标签 + 回填 parent:/去旧别名
python cli.py auto [--limit N] [--autotag]   # 自动处理链:封面→OCR→镜头→pHash→(打标)→语义索引(幂等)
python cli.py shots <id> [--force] | shots --all [--limit N]   # 镜头索引(ffmpeg 场景检测)
python cli.py phash <id> [--force] | phash --all [--limit N]   # dHash 感知哈希
python cli.py similar <id> [--max-dist N]   # 画面级近重复检测(汉明距离升序)
python cli.py near-dupes [--max-dist N] [--limit N]   # 全库近重复报告(对+簇)
python cli.py imgsearch <id|path> [--max-dist N] [--mode phash|clip] | imgsearch --text "厨房"   # 以图/以文搜图
python cli.py imgembed [--force] [--limit N] [--status]   # CLIP 图像向量索引
python cli.py govern [--limit N]   # 治理/合规扫描(占位/缺描述/未分类/无标签)
python cli.py readiness --job <id>   # 某 job 分发渠道就绪度
python cli.py feedback --action X [--reject] [--note Y]   # 记录 Agent 动作采纳/否决(学习闭环)
python cli.py learning [--limit N]   # 汇总反馈学习日志(各动作采纳率)
python cli.py package "目标" [--kind videos] [--scope master|clips|all] [--limit N]   # 目标→素材包组装(只读资产,写 manifest 到 agent_workspace/packages/)
python cli.py deliver --manifest <pkg.json> [--ids id1,id2] [--fmt mp4] [--res 720|1080|0] [--out-dir DIR] [--copy-only] [--overwrite] [--confirm]   # 按需交付(转码/裁剪);默认 dry-run,--confirm 才写文件
python cli.py publish "目标" [--confirm] [--fmt mp4] [--res 720|1080|0]   # 一键出片:先组装素材包再导出交付变体(串联 package→deliver);默认 dry-run
python cli.py agent --task "..." [--write] [--max-steps 12] | --status <id> | --cancel <id> | --resume <id> [--extra-steps 6] | --list | --cleanup [--max-age 72]   # Deep Agent 多步任务
```

## 联动 subtitle_pipeline(快速上手)
只读扫描,不搬文件,自动归类 + 打标签 + 登记(详见 `INTEGRATION.md`)。
```bash
python bridge_subtitle.py --dry-run                      # 先统计各 scope 数量,不登记
python bridge_subtitle.py                                # 登记全部(幂等,sha256 去重)
python bridge_subtitle.py --scope batch                 # 只扫 downloads/batch
python bridge_subtitle.py --scope benchmarks             # 只扫基准视频
python bridge_subtitle.py --root <SP_ROOT>               # 指定 subtitle_pipeline 根
python bridge_subtitle.py --limit 20                     # 试登记前 20 个
python bridge_subtitle.py --watch                        # 轮询守护:每 30s 增量登记新素材(Ctrl+C 退出)
python bridge_subtitle.py --watch --interval 60 --scope batch  # 自定义间隔/范围
python bridge_subtitle.py --thumbs                       # 登记后给新增视频补封面
python bridge_subtitle.py --prune                        # 顺带清理失效的外部引用
python bridge_subtitle.py --state                        # 打印上次同步状态
python bridge_subtitle.py --thumbs --prune               # 推荐:一次跑完 登记 + 封面 + 巡检
```
- **幂等**:重复运行会跳过已登记(同 sha256)的素材。
- **重做**:想清空重建,删 `index/hub.db` 后重跑 `python bridge_subtitle.py`(仅重建外部引用;`materials/` 下的内部素材需再 `python cli.py scan`)。
- **当前状态**(2026-10-06 实测,重建后):共 **384** 条,其中 **370** 条为外部引用、14 条内部素材(外部引用 0 项失效);
  按 kind:images 157 / docs 105 / silent 77 / subs 19 / videos 16 / audio 8 / anim 1 / other 1。
  *(2026-10-05 曾因索引重置掉到 23 条、评估门禁失效;重跑 `python bridge_subtitle.py` 后恢复为 384 条,语义向量覆盖率 0/384 待重建。)*
- **定时 / 流水线触发**:可在 batch 跑完后定时执行 bridge;进阶可在 subtitle_pipeline 收尾调用 `--scope batch`,或经其 `vitual_mcp` 推送(MCP 阶段二)。

## Python API(core)
```python
import core
core.init_hub()
core.classify(path)                 # -> 'images'/'videos'/...
core.compute_sha256(path)           # -> hex
core.sanitize_name(name)            # 命名规范化
core.ingest_file(src, move=True)    # 整理单个文件(去重/分类/命名/入库)
core.ingest_dir(dirpath)            # 整理目录
core.ingest_external(src, source, tags, description)  # 外部引用登记(不复制)
core.scan_materials()               # 扫描 materials/ 全树补录
core.search(q="", kind="", tag="", limit=None, offset=0)  # 检索(可分页);q 为空按筛选+时间倒序
core.count_materials(q, kind, tag)  # 当前筛选命中总数(配合分页)
core.query_materials(q, kind, tag)  # 不分页的完整结果
core.all_materials()                # 全部(不分页,供统计/导出)
core.get_material(mid)              # 单条
core.duplicates()                   # 重复组
core.update_tags(mid, tags)
core.update_description(mid, desc)
core.remove_material(mid)           # 内部移 trash,外部只删索引
core.ffmpeg_path()                  # 自动发现的 ffmpeg 路径(无则 None)
core.make_thumb(mid)                # 抽帧生成视频封面(缓存;失败留 .fail 标记)
core.thumbs_status()                # {ffmpeg, exe, cached, failed, dir}
core.missing_thumbnail_ids()        # 还没有封面的视频 id(默认跳过已知损坏)
core.purge_thumbs()                 # 清空封面缓存与失败标记
core.embed_status()                 # 语义索引状态(模型/已索引/覆盖率)
core.build_embeddings(force=False)  # 增量构建语义索引
core.expand_query("去除字幕")        # 中文 → 语料英文词的同义词扩展
core.search(q, mode="lexical")      # 强制纯词法;mode="semantic" 强制语义
core.external_stats()               # {internal, external, broken}
core.broken_externals()             # 失效的外部引用列表
core.prune_broken_externals()       # 清理失效引用索引(不动磁盘)
core.health()                       # 健康快照(总量/种类/重复/引用/封面/体积)
```

## HTTP API(server.py)
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` · `/index.html` | 管理面板 |
| GET | `/api/list?q=&kind=&tag=&limit=&offset=&mode=` | 检索 / 列表(`limit` 省略=不分页;`mode=auto\|lexical\|semantic`;视频项带 `thumb` 标记) |
| GET | `/api/count?q=&kind=&tag=&mode=` | 当前条件下命中总数(配合分页算总页数) |
| GET | `/api/stats` | 总数 / 重复组 / 各 kind 数量 / 封面状态 / 批量任务进度 / 外部引用与失效数 |
| GET | `/api/health` | 健康检查快照(`ok` / 引用完整性 / 封面进度 / 索引体积),适合监控轮询 |
| GET | `/api/broken` | 失效外部引用明细(原文件已不存在) |
| GET | `/api/dupes` | 重复文件组 |
| GET | `/api/tags` | 全部标签 + 计数(面板标签下拉) |
| GET | `/api/file/<id>` | 预览原文(外部引用读 `external_path`,支持 HTTP Range 流式) |
| GET | `/api/thumb/<id>` | 视频封面 jpg(命中缓存直接返回,否则现场抽帧;失败返回 404 + 原因) |
| GET | `/api/export?fmt=json\|csv` | 导出全部编目(带下载文件名;CSV 含 BOM) |
| POST | `/api/upload` | 上传(表单 multipart) |
| POST | `/api/ingest` | 整理 `ingest/` |
| POST | `/api/scan` | 扫描 `materials/` 全树 |
| POST | `/api/thumbs` | `{}` 后台批量抽帧;`{"limit":N}` 限量;`{"purge":true}` 清缓存与失败标记 |
| POST | `/api/embed` | `{}` 增量构建语义索引;`{"force":true}` 全量重建;`{"limit":N}` 限量(后台任务,进度见 `/api/stats` 的 `embed.job`) |
| POST | `/api/tag` | `{id, tags}` 更新标签 |
| POST | `/api/describe` | `{id, description}` 更新描述 |
| POST | `/api/remove` | `{id}` 删除 |
| POST | `/api/prune` | 清理全部失效外部引用的索引(只删索引,不动磁盘) |

## 命名规范
非字母数字 / 中文 / 连字符统一为 `-`,折叠连续 `-`,截断至 80 字符 + 小写扩展名。原始文件名保留在 `orig_name` 字段。

## 运维:备份 / 重建 / 迁移
`index/hub.db` 是**派生索引(derived index)**,不是唯一真相源——真相是磁盘上的原文件。因此:
- **备份**:优先备份原始素材文件;`hub.db` 可随时重建,不必单独备份。需要可移植快照时用面板「导出 JSON / CSV」(`/api/export`)。
- **重建**:索引损坏或想重置,直接删 `index/hub.db`,然后
  `python bridge_subtitle.py`(重建外部引用)+ `python cli.py scan`(重建内部素材)。`sha256` 保证幂等,不会重复。
  `index/thumbs/`(封面缓存)与 `embeddings` 表(语义向量)同样是派生的:删掉后重新「生成封面」/`python cli.py embed` 即可。
- **迁移**:把 `materials/` 与原项目一起拷贝,重跑上面的重建命令即可,无需迁移数据库。
> 这条"索引可丢弃、可重建"的原则,是本地素材库能长期稳定运维的关键。

## 语义检索(本地 embedding)
**前提**:本机有 ollama 且装了 embedding 模型(离线即可)。默认自动发现带 `embedding` 能力的模型,
例如 `ollama pull nomic-embed-text`(768 维,约 270MB);也可用 `VITUAL_EMBED_MODEL` 指定。
没有模型时**不报错**,检索自动退回纯词法(面板「构建语义索引」按钮会置灰并在状态栏写明原因)。

**建索引**(增量,文档文本没变就跳过):
```bash
python cli.py embed              # 或面板「构建语义索引」按钮
python cli.py embed --force      # 全量重建
python cli.py embed --status     # 只看状态
python bridge_subtitle.py --embed          # 随同步流程一起刷新(新素材立刻可搜)
python bridge_subtitle.py --watch --thumbs --embed --prune   # 一条命令全自动
```

**混合排序怎么做的**
1. 向量以 float32 BLOB 存在 `embeddings` 表(带文档指纹,支持增量重建);
2. 查询时对**整个(kind/tag 过滤后的)语料**算稠密相似度,**减去语料均值向量再算余弦**(去均值居中)——
   不做这步的话句向量各向异性严重(本库实测两两余弦均值 0.76),"什么都像什么";
3. 稠密排名与词法排名用 **RRF 融合**(词法权重默认 1.0),因此**语义只负责补召回,不会埋掉精确关键词命中**;
4. 结果集 = 稠密相似度过线(默认 0.25)∪ 词法命中的,再按融合分排序。

**实测(历史演进在 9 宽查询上测,2026-09-28 起权威口径为 16 查询人工标注 ground truth)**

> **语料现状(2026-10-06 实测)**：索引曾因重置掉到 **23 条**、评估门禁失效；已重跑 `bridge_subtitle.py` 重建为 **384 条**，`eval_ground_truth.json` 的 **233 个 id 现存 233 个（100%）**，**门禁恢复可复现**。当前实测低于 344 语料期的 0.86，**归因是语料增长（344→384）+ GT 按旧语料标注导致新增相关项未标注（如 31 条 `type:benchmark` 中 GT 仅标 18 条），而非检索代码回归**；要回到 0.86 量级需按当前语料重标注/扩充 GT。

| 阶段 | P@5 | R@20 |
|---|---|---|
| 原始词法(中文查询) | 0.00 | 0.00 |
| + 领域同义词表 | 0.44 | 0.52 |
| + 长词松匹配 | 0.53 | 0.63 |
| + 语义混合 | 0.71 | 0.71 |
| + 同义词补齐 & 中文 bigram 降权(9 宽查询) | 0.96 | 0.69 |
| **历史归档:16 查询 GT(2026-09-28,344 条语料)** | **0.86** | **0.82** |
| **当前实测(2026-10-06,384 条语料,lexical)** | **0.60** | **0.78**(MRR 0.75 / NDCG@10 0.75) |

> 16 查询口径含 7 组窄查询(相关集 1–6 条),并新增 **MRR=1.00 / NDCG@10≈0.98**:全部查询 top1 必对、
> 窄查询召回全进 top20。为什么权威口径 P@5 反而比 0.96 低?——9 宽查询相关集最大 87 条,指标饱和虚高;
> 窄查询才对"无关条目混进 top5"敏感。详见 `eval_search.py` 与《最佳实践》§15.4。

个别查询收益更明显:「把模糊画面变清晰」0.20 → 0.80(P@5)、「按时间切的片段」0.00 → 0.16(R@20)。

**环境变量**

| 变量 | 默认 | 说明 |
|---|---|---|
| `VITUAL_EMBED_URL` | `http://127.0.0.1:11434` | ollama 地址 |
| `VITUAL_EMBED_MODEL` | 自动发现 | 指定 embedding 模型 |
| `VITUAL_EMBED_MIN_COS` | `0.25` | 稠密相似度入选阈值(居中后) |
| `VITUAL_RRF_K` / `VITUAL_HYBRID_WLEX` | `60` / `1.0` | RRF 常数 / 词法权重(调大更偏精确关键词) |
| `VITUAL_RERANK_MODEL` | 空(关闭) | 可选 stage-2 重排:设 `bge-reranker-v2-m3` 走 ollama cross-encoder;`lexical` 为内置离线弱基线(实测会劣化排序,勿用于生产);`VITUAL_RERANK_TOP` 控制精排候选量(默认 60) |
| `VITUAL_CACHE_SEARCH` / `VITUAL_CACHE_PERSIST` | 关 / 关 | 查询内存缓存 / 额外落盘 `index/search_cache.pkl`(跨会话复用) |
| `VITUAL_OCR_PYTHON` | 自动发现 | 画面 OCR 用的 python(需装 rapidocr;默认自动找 `subtitle_pipeline/.venv`) |

**已知限制(实测结论,别指望它做多语检索)**
- `nomic-embed-text` 是**英文单语**模型,中文查询真正起作用的是**同义词表 + 松匹配**这一层;
  换多语模型(如 `bge-m3`)后中文语义会更好。
- 中文单字匹配会带来少量误召回(例如查询里的「赛」命中了无关的中文文件名)。
- 语义检索需要 ollama 常驻;关掉后自动回退词法,不影响使用。
- 想只用精确关键词:面板检索方式切「精确关键词」,或 `?mode=lexical`。

## 引用完整性与健康检查
外部引用的代价是「索引可能指向已经不存在的文件」——上游清理产物、移动目录后就会发生。素材中心把这件事当一等公民对待:

| 层级 | 表现 |
|---|---|
| 数据层 | `core.broken_externals()` 巡检、`core.prune_broken_externals()` 清理、`core.health()` 快照 |
| 接口层 | `/api/stats`(`external.broken`)、`/api/broken`(明细)、`/api/health`(`ok:false`)、`POST /api/prune` |
| 界面层 | 状态栏红字「失效 N」可点击 → 弹窗列出失效项 → 「清理全部」 |
| 卡片层 | 失效素材的卡片直接显示红色「原文件缺失」,不再发起必然 404 的预览请求 |
| CLI 层 | `bridge_subtitle.py --prune`(巡检并清理)、`--prune --dry-run`(只看不删) |

**安全原则**:清理**只删索引记录,绝不删除或修改任何磁盘文件**。原文件恢复后再跑一次 bridge 即可重新登记(sha256 幂等)。
**健康检查**:`GET /api/health` 返回 `ok`(引用是否完整)、总量与种类分布、重复数、索引体积、封面进度,可直接接监控轮询。

## 预览与缩略图策略
- **图片**:直接返回,前端缩略图展示。
- **视频 / 音频**:`/api/file/<id>` 支持 **HTTP Range**,浏览器内嵌播放器可拖拽进度、边下边播(大文件也不整块读内存)。
- **外部引用**(subtitle_pipeline 大视频)预览同样走 Range,不复制文件。

### 视频封面(双模式自动降级)
| 模式 | 触发条件 | 行为 | 代价 |
|---|---|---|---|
| **A 服务端抽帧**(首选) | 找到 ffmpeg | `/api/thumb/<id>` 用 ffmpeg 抽一帧存 `index/thumbs/<id>.jpg`,命中缓存直接返回;前端作 `<video poster>` | 首次每视频约 0.1–1s,之后走缓存 |
| **B 浏览器截帧**(兜底) | 找不到 ffmpeg | 前端把 `<video>` seek 到 10% 处 → `canvas` 抓帧 → `toDataURL` 当 poster | 每个视频需加载一次,零服务端成本 |

两种模式都**进视口才取封面**(`IntersectionObserver`),避免一次列出上百个视频时同时拉流/抽帧。

**ffmpeg 自动发现**:很多 Python 包自带 ffmpeg 二进制(只是不在 PATH 上),因此探测顺序为
`VITUAL_FFMPEG` 环境变量 → `PATH` → 同工作区项目的 `*/.venv/Lib/site-packages/{static_ffmpeg,imageio_ffmpeg}/.../ffmpeg.exe` → `C:\ffmpeg\bin\ffmpeg.exe`。
> 本项目即靠这条规则免安装获得抽帧能力:命中 `subtitle_pipeline/.venv` 里的 `static_ffmpeg` 自带 ffmpeg。

**损坏源文件不反复重试**:抽帧失败的会留下 `<id>.jpg.fail` 标记(内含 ffmpeg 报错摘要),后续不再白跑;面板在该视频上标「封面不可用」。
想重试:面板 `POST /api/thumbs {"purge":true}` 清掉缓存与标记,再重新生成。

**相关环境变量**
| 变量 | 默认 | 说明 |
|---|---|---|
| `VITUAL_FFMPEG` | 空 | 指定 ffmpeg 可执行文件(优先级最高) |
| `VITUAL_THUMB_SEEK` | `1` | 抽帧时间点(秒);避开片头黑帧,失败自动回退 0 秒 |
| `VITUAL_THUMB_CONCURRENCY` | `2` | 同时抽帧进程上限(限流,避免一次拉起几十个 ffmpeg) |

**批量生成**:面板「生成封面」按钮,或 `POST /api/thumbs`(后台线程 + 状态栏实时进度)。
实测 83 个视频(含 186MB 大文件)→ 75 张封面,**12.1s** 完成。

## 已知限制与路线图
**已知限制**
- **本地单用户(现已可鉴权)**:服务默认只听 `127.0.0.1`;设 `VITUAL_HUB_TOKEN` 后开启令牌鉴权,可安全跨机(见下方「安全与网络暴露」)。
- **无版本管理**:素材更新会覆盖索引记录,不保留历史版本。
- **语义检索依赖本地 ollama**:没有 embedding 模型时自动退回纯词法;当前可用模型是英文单语的,中文靠同义词表兜(见[语义检索](#语义检索本地-embedding))。
- **封面依赖 ffmpeg**:本机找不到 ffmpeg 时自动降级为浏览器截帧(可用但较慢);源文件损坏(如上游写入中断的 mp4)抽不出封面,只会标记不重试。
- **封面缓存不入库**:`index/thumbs/` 是派生数据,删除无副作用(重跑生成即可)。

## 安全与网络暴露(上线前必读)

素材中心默认**只听本机 `127.0.0.1`**,未设口令时等同于本地开放(适合本机/可信局域网)。
一旦要离开可信环境,必须同时做两件事:

1. **设口令**:环境变量 `VITUAL_HUB_TOKEN=<口令>`。设完后,Web 面板与所有 `/api/*` 请求都需带令牌
   (浏览器访问 `http://localhost:8000/?token=<口令>`;API 用 `Authorization: Bearer <口令>` 头),否则返回 401。
   MCP 端则需在客户端配置里传一致的 `--token`。
2. **换监听地址**:默认 `127.0.0.1`;仅当确需跨机访问时才设 `VITUAL_HUB_HOST=0.0.0.0`,且**务必先设 token**。

**外部引用防越权读取**:`ingest_external` 登记时,以及 `/api/file` 读取前,都会校验 `external_path`
落在允许根内(默认=素材中心所在工作区,即 `materials_hub` 的上一级;跨工作区素材用 `VITUAL_HUB_EXT_ROOTS`
追加,分号分隔)。越界路径一律拒绝(`rejected` / 403),即使索引库被写坏也读不到工作区外的任何文件。

| 变量 | 默认 | 说明 |
|---|---|---|
| `VITUAL_HUB_TOKEN` | 空 | 设了即开启令牌鉴权(Web + API + MCP 共用) |
| `VITUAL_HUB_HOST` | `127.0.0.1` | 监听地址;跨机才改 `0.0.0.0`(且仅配合 token) |
| `VITUAL_HUB_EXT_ROOTS` | 工作区根 | 允许登记/读取外部引用的额外根目录(分号分隔) |

**后续可扩展**
- **多语 embedding / 图片语义**:换成 `bge-m3` 等多语模型可显著改善中文语义;再用 CLIP 类模型可支持"按画面内容搜图"(当前语义只覆盖元数据文本)。
- **转码代理**(依赖 ffmpeg):为超大视频生成低码率预览版,提速浏览。
- **远程采集**:加网络抓取源(需明确站点与授权)。
- **多库隔离**:按项目分库。
- **MCP 推送**:由 subtitle_pipeline 的 `vitual_mcp` 把产物推送到素材中心(阶段二)。

## FAQ
- **为什么视频不复制进素材中心?** 体量大(subtitle_pipeline 下 177+ 个 mp4),复制会重复占盘。采用「原路径引用」,只登记索引,预览按需读取原文件。
- **删除素材会删掉原文件吗?** 不会。内部素材删除后移入 `trash/`(可找回);外部引用(subtitle_pipeline)删除只删索引,原文件毫发无损。
- **怎么重新登记 / 重建索引?** 删 `index/hub.db`,重跑 `python bridge_subtitle.py` + `python cli.py scan`(幂等)。
- **索引库坏了影响原文件吗?** 不影响。`hub.db` 是派生索引,可随时丢弃重建(见运维)。
- **外部文件被上游删了/搬走了怎么办?** 状态栏会红字提示「失效 N」,点开弹窗可一键清理;命令行用 `python bridge_subtitle.py --prune`(加 `--dry-run` 只看不删)。清理只删索引记录,**不会删任何磁盘文件**。
- **怎么判断索引指向的文件都还在?** `GET /api/health` 的 `ok` 字段即答案(或看状态栏是否显示「引用完整」)。
- **能用自然语言搜吗?** 能。默认走「词法(同义词+松匹配)+ 语义」混合排序,问句不必含连续子串;精确过滤用 `type:` / `job:` / `lang:` 标签。
- **语义检索要不要额外装东西?** 不用装 Python 包。只要本机 ollama 里有 embedding 模型(`ollama pull nomic-embed-text`)即可,全离线;没有就自动退回词法。
- **为什么中文查询要靠同义词表?** 因为手边能离线拿到的 embedding 模型(nomic-embed-text/bge-m3 建库前)是英文单语的,而本库元数据也是英文;词表把中文意图映射到语料词汇。历史(2026-09-28,16 查询人工标注,344 条语料):**P@5=0.86 / R@20=0.82 / MRR=1.00 / NDCG@10≈0.98**。**2026-10-06 复测**:已重建索引至 384 条、GT 233 个 id 100% 命中,门禁恢复可复现;当前实测(lexical)**P@5 0.60 / R@20 0.78 / MRR 0.75 / NDCG@10 0.75**(低于历史 0.86 系语料增长+GT 标注不全所致,非代码回归,详见上节)。语义向量覆盖率 0/384(未重建;历史实测语义边际增益 lift≈0)。注意:英文单语模型下语义检索的边际增益≈0(16 查询实测 lift≈0),换多语 chat/embedding 组合或 cross-encoder 重排(`VITUAL_RERANK_MODEL`)才可能再抬;重排启用前必须过评估门禁——内置 `lexical` 弱基线实测会把 P@5 从 0.86 打到 0.31。
- **检索结果变了?** 若新增了大量素材,记得 `python cli.py embed`(或面板按钮 / `bridge --embed`)刷新语义索引;未建向量的条目仍走词法,不会丢。
- **视频封面是怎么来的?** 服务端用自动发现的 ffmpeg 抽第 1 秒的帧,存 `index/thumbs/<id>.jpg` 复用;本机确实没有 ffmpeg 时,前端用 `<video> + canvas` 截帧兜底。两种模式都只对进入视口的卡片生效。
- **为什么有几个视频显示「封面不可用」?** 那些源文件本身损坏(典型报错 `moov atom not found`,多为上游写入中断的 mp4)。抽帧失败会留 `.fail` 标记不再重试;**源文件修好后**用 `POST /api/thumbs {"purge":true}` 清标记再生成即可。


## MCP 接入(阶段二,零依赖 stdio server)

`mcp_server.py` 让 AI 助手(Claude Desktop / CodeBuddy 等)通过 MCP 直接检索素材库。

注册(配置文件里加):
```json
{"mcpServers": {"materials-hub": {
  "command": "python",
  "args": ["d:/MineWeb/2026/Vitual/materials_hub/mcp_server.py", "--token", "<TOKEN>"],
  "env": {"VITUAL_HUB_TOKEN": "<TOKEN>"}}}}
```
> **鉴权**:一旦设了 `VITUAL_HUB_TOKEN`,MCP 客户端必须在 `--token` 里传一致的令牌,否则 server 启动即拒绝(`sys.exit(2)`)——防止本机任意进程无鉴权调起素材中心 Agent 接口。

工具(基础 24 个 + Deep Agent 类 5 个 = **29 个**,完整签名见 `mcp_server.py`):
`search_materials(q,kind?,tag?,mode?,limit?)` 中文自然语言检索 /
`get_material(id)` / `list_tags(limit?)` / `hub_stats()` /
`chunk_search_materials(q,limit?)` 长文档父子分块检索(覆盖 .md/.txt/.srt 关联文本全文)/
`read_text_preview(id,limit?)` 只读预览描述与关联文本前 N 字符(省 token)/
`run_ocr(id,confirm)` 视频画面 OCR 离线识别(rapidocr,文本入检索)/
`get_shots(id)` 只读镜头时间轴[{start,end},...] /
`auto_process(confirm,limit?,autotag?)` 对新素材跑封面/OCR/镜头/pHash(写,需 confirm) /
`find_similar(id)` 画面近重复(dHash) /
`near_duplicate_report(max_dist?,limit?)` 全库近重复对+簇 /
`search_by_image(query|id|path)` 以图搜图(dHash;CLIP 待权重) /
`related(id,rel?)` 关系反查(沿 parent:/role:/job: 面标签一跳遍历,rel=all/parent/children/job/role/kind) /
`build_image_embeddings(force?,limit?,status?)` CLIP 图像向量索引(以文/以图搜图) /
`list_missing_covers` / `job_checkup` 运维只读技能 /
`governance_report(limit?)` 全库治理/合规扫描(占位/缺描述/未分类/无标签) /
`distribution_readiness(job_id?)` 某 job 分发渠道就绪度(channel_ready + blocking) /
`agent_feedback(action,accepted[,note?,by?])` 记录 Agent 动作采纳/否决(学习闭环,追加写) /
`learning_summary(limit?)` 汇总反馈学习日志(各动作采纳率) /
`assemble_package(goal,kind?,scope?,limit?,queries?)` 目标→素材包组装(多智能体 Librarian→Critic→Executor 角色链;只把 manifest 写到 agent_workspace/packages/,只读资产) /
`deliver_package(manifest?,ids?,fmt?,res?,clips?,out_dir?,copy_only?,overwrite?,confirm(必须 true))` 把素材包/指定 id 导出为下游交付变体(视频类转码/区间裁剪/格式归一,**非视频类按原样复制保留原扩展名**;只新建文件、绝不改动资产本体;写需 confirm) /
`update_tags(id,tags,confirm[,remove])` 与 `register_asset(path,...,confirm)` **写工具(强制 `confirm=true`)**；`update_tags` 为**合并语义**(只增不删，系统面标签永不动，删须 `remove`)。
Deep Agent 类(另见下节):`agent_run` / `agent_status` / `agent_cancel` / `agent_list` / `agent_resume`。

Resources(订阅式只读,比 tool 更省 token):`hub://recent/{n}` 最新素材、`hub://job/{jobid}` 某 subtitle_pipeline job 全套、
`hub://history/{n}` **写操作审计日志**、`hub://shots/{id}` **镜头索引**(未建则 `{status:missing}`)、
`hub://agent/{task_id}` **Agent 任务实时状态**(待办/步骤/总结/citations/critic/context_digest,等价于 `agent_status` 工具)、
`hub://related/{id}/{rel}` **关系反查**(沿 `parent:`/`role:`/`job:` 面标签一跳遍历,rel 取 all/parent/children/job/role/kind);
Prompts:`fill_missing_thumbs`、`job_checkup`（`prompts/list` / `prompts/get`）；
只读技能工具:`list_missing_covers`、`job_checkup`。
`initialize` 按运行环境动态声明能力(`serverInfo.hub` 透出 semantic/reranker/token_required/count 实时标志)。
启动时会顺手自动拉起 ollama(失败不影响词法检索)。日志走 stderr,协议走 stdout。

### 事件驱动(面板/API)

上传、整理 ingest、扫描、`POST /api/split-silent` 成功后会后台 `enqueue_autoproc`
(thumb→OCR→shots→pHash);进度看 `GET /api/auto/status`,也可 `POST /api/auto` 手动跑。
流水线 `hub_push` → `run_job_dir`：登记后默认拆 silent+track 再 auto。
详解与路线图见 `素材中心最佳实践与优化分析.md` §18–§22；Cursor skill：`materials-hub-agent`。

## Deep Agent(路线 C:stdlib 编排 + MCP 暴露)

`agent.py` 是 **Deep Agent 架构**(2026-09-28 拍板,区别于单轮 tool-calling)的核心编排层——
LLM 先把任务拆成待办,再逐步调用素材工具完成,长观察落盘、子代理隔离上下文。四支柱:

| 支柱 | 实现 |
|---|---|
| **Planning tool** | 任务先由 LLM 拆成 3–6 条 todos,逐步推进;状态持久化 `index/agent_workspace/<task_id>/state.json`,随时 `agent_status` 查轨迹 |
| **上下文卸载** | 观察 >1200 字符落盘 `<ws>/notes/step_NNN.json`,上下文只留指针+前 200 字符(与 `chunk_search`/`read_text_preview` 同一省 token 哲学) |
| **Subagents** | `retrieve`(LLM 驱动:多查询→汇总,只回摘要)与 `maintain`(确定性巡检 health+broken,零幻觉不走 LLM) |
| **详细系统提示** | 角色+工具表+规则齐备;**写工具默认不存在**,仅 `allow_write=True` 才注册(MCP 侧再叠 `confirm=true` 人工复核,双重护栏) |

- **LLM 后端**:本机 ollama chat 模型(与 auto_tag 同一发现逻辑);无 chat 模型 → `skipped` 优雅降级。
- **MCP 工具(agent 类 5 个:agent_run / agent_status / agent_cancel / agent_list / agent_resume;完整 29 个 MCP 工具见上方「MCP 接入」)**:`agent_run(task, confirm?, max_steps?)` 默认只读,写任务需 `confirm=true`;`agent_status(task_id)` 查待办/步骤/总结;`agent_cancel(task_id)` **协作式取消**——写 `cancel.flag`,主循环下一步边界终止,已执行进度保留落盘(长任务 1–3 分钟不必干等);`agent_list(limit?)` 盘点历史任务(状态/步数/创建时间);`agent_resume(task_id, extra_steps?)` **续跑步数耗尽的任务**——复用待办/历史步骤/滚动摘要,接续步号不重做(真实库历史任务约半数 max_steps_reached,7B 常来不及 finish)。
- **CLI**:`python cli.py agent --task "..." [--write] [--max-steps 12]` / `--status <id>` / `--cancel <id>` / `--resume <id> [--extra-steps 6]` / `--list` / `--cleanup [--max-age 72]` / `--file task.txt`(中文规避终端 GBK)。
- **为什么是"路线 C"**:官方 `deepagents` 库依赖 langchain/langgraph,与零依赖哲学冲突;故编排自实现、协议走既有 MCP——未来可无缝切官方库或接入 CodeBuddy/Claude 等宿主。
- **离线测试**:`tests/test_agent.py`(单元 + mock LLM 脚本回放)、`tests/test_agent_skills.py`(MCP 工具/资源/技能冒烟)、`tests/test_agent_eval.py`(真实任务模板回归,断言无编造 id/引用合法/记忆沉淀/长任务压缩)、`tests/test_deliver.py`(按需交付 dry-run/confirm/缺失 id/命令构造/技能路由 + 一键出片串联);全套件 `pytest` 当前 **121 passed**(均 mock 驱动,不连 ollama)。

### 入口与调用链(2026-09-30 补)

分两层,宿主只跟 MCP 层打交道:

1. **进程入口(服务)**:`mcp_server.py` 的 `main()`(`mcp_server.py:517`)——MCP **stdio JSON-RPC** 服务器:`sys.stdout/in` 重定向 UTF-8 → `core.init_hub()` → `while True: readline()` 行循环 → `_dispatch(req)` 分发。由宿主(CodeBuddy / Claude Desktop)按 `mcpServers` 配置以 `python mcp_server.py --token <TOKEN>` 拉起(见上方「MCP 接入」段)。
2. **逻辑入口(agent 本体)**:`agent.py` 的 `agent_run(task, allow_write, max_steps, model, task_id)`(`agent.py:478`)——Deep Agent 主循环,返回最终状态 dict;轮询用 `agent_status(task_id)` 读取 `index/agent_workspace/<task_id>/state.json`。底层 LLM=本机 ollama,无模型时 `skipped` 降级(零网络依赖)。

**调用链**:宿主调 MCP 工具 `agent_run` → `t_agent_run`(`mcp_server.py`)丢**后台线程**跑 `agent.agent_run()`,立即返回 `task_id` → 宿主轮询 `agent_status` 看进度/结果(长任务 1–3 分钟不阻塞 MCP 调用);中途可 `agent_cancel(task_id)` 协作式取消(步边界终止、进度保留)。

### 2026-09-30 对照优化(六大缺口已全部闭环)

参照 2026 Agentic RAG / Deep Agent / Agent Memory 在线实践,`素材中心最佳实践与优化分析.md` §20 记录完整对照,`agent.py`/`mcp_server.py` 已落地六项(均模型无关、本地 7B 友好,不依赖更强模型):

| 缺口 | 落地要点 |
|---|---|
| 1 后台异步执行 | `t_agent_run` 后台线程 + 主循环每步 `_save` 落盘,支持实时进度轮询/可取消 |
| 2 finish 核验 + 诚实弃权 | `seen_ids` 全程白名单,finish 声明 id 越界即剔除并标 `[核验]`;补第 8 条诚实规则 |
| 3 跨任务 playbook 记忆 | `index/agent_workspace/agent_memory.json` 沉淀「查询词→有效 facet」规律,下次注入提示 |
| 4 确定性 Critic + 引用溯源 | `citations:[{claim,id}]` 逐条校验 + 正文正则 `[0-9a-f]{12}` 扫描标 `[Critic]` 剔除幻觉 id |
| 5 关系反查工具 | `related(id, rel)` 沿 `parent:/role:/job:` 面标签一跳遍历(确定性) |
| 6 动态上下文压缩 | 尾窗口外步骤压入滚动摘要 `context_digest` 回灌 prompt,防本地 7B 长任务遗忘/context-rot |

测试:`tests/test_agent*.py` 全套件 `pytest` **100 passed**(mock LLM 回放,不连 ollama)。
