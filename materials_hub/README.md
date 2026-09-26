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
sp | <platform> | job:<id> | type:<media|subs|notes|benchmark|render|test> | lang:<xx>
```
- `sp` —— 来源标记(固定值,表示来自 subtitle_pipeline)
- `<platform>` —— bilibili / youtube 等(受控:只填已知平台)
- `job:<id>` —— 对应某次采集任务(同一任务的全套素材可一键筛出)
- `type:<...>` —— 业务类型(受控枚举,见上)
- `lang:<xx>` —— 从文件名识别的语言(zh/en/ja…)

**扩展规则(保持一致,便于检索)**:
1. 维度用 `key:value` 形式(`job:`、`type:`、`lang:`),便于分面过滤;自由文本放进 `description`,别塞进 tags。
2. 新增类型只扩 `type:` 的受控枚举,不要发明新前缀;新平台加到 `<platform>` 白名单。
3. 标签统一小写;多个标签用逗号分隔,顺序无关(检索按集合匹配)。
4. 手动上传的素材同样建议套用 `type:` / `lang:` 维度,保持全库一致。

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
- **当前状态**(最近一次全量):共 **344** 条,其中 **341** 条为外部引用 subtitle_pipeline;
  按 kind:videos 83 / images 157 / docs 85 / subs 16 / audio 3;
  按 type:render 240 / notes 30 / subs 16 / media 23 / benchmark 18 / test 4。
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

**实测(本库 344 条真实素材,9 个中文查询,ground truth 按真实目录/类型标注)**

| 阶段 | P@5 | R@20 |
|---|---|---|
| 原始词法(中文查询) | 0.00 | 0.00 |
| + 领域同义词表 | 0.44 | 0.52 |
| + 长词松匹配 | 0.53 | 0.63 |
| **+ 语义混合** | **0.71** | **0.71** |

个别查询收益更明显:「把模糊画面变清晰」0.20 → 0.80(P@5)、「按时间切的片段」0.00 → 0.16(R@20)。

**环境变量**

| 变量 | 默认 | 说明 |
|---|---|---|
| `VITUAL_EMBED_URL` | `http://127.0.0.1:11434` | ollama 地址 |
| `VITUAL_EMBED_MODEL` | 自动发现 | 指定 embedding 模型 |
| `VITUAL_EMBED_MIN_COS` | `0.25` | 稠密相似度入选阈值(居中后) |
| `VITUAL_RRF_K` / `VITUAL_HYBRID_WLEX` | `60` / `1.0` | RRF 常数 / 词法权重(调大更偏精确关键词) |

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
- **本地单用户**:服务监听 `0.0.0.0:8000` 但无认证/权限;仅本机或可信局域网使用,勿直接暴露公网。
- **无版本管理**:素材更新会覆盖索引记录,不保留历史版本。
- **语义检索依赖本地 ollama**:没有 embedding 模型时自动退回纯词法;当前可用模型是英文单语的,中文靠同义词表兜(见[语义检索](#语义检索本地-embedding))。
- **封面依赖 ffmpeg**:本机找不到 ffmpeg 时自动降级为浏览器截帧(可用但较慢);源文件损坏(如上游写入中断的 mp4)抽不出封面,只会标记不重试。
- **封面缓存不入库**:`index/thumbs/` 是派生数据,删除无副作用(重跑生成即可)。

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
- **为什么中文查询要靠同义词表?** 因为手边能离线拿到的 embedding 模型(nomic-embed-text)是英文单语的,而本库元数据也是英文;词表把中文意图映射到语料词汇,实测把中文查询 P@5 从 0.00 拉到 0.44,再叠加语义到 0.71。
- **检索结果变了?** 若新增了大量素材,记得 `python cli.py embed`(或面板按钮 / `bridge --embed`)刷新语义索引;未建向量的条目仍走词法,不会丢。
- **视频封面是怎么来的?** 服务端用自动发现的 ffmpeg 抽第 1 秒的帧,存 `index/thumbs/<id>.jpg` 复用;本机确实没有 ffmpeg 时,前端用 `<video> + canvas` 截帧兜底。两种模式都只对进入视口的卡片生效。
- **为什么有几个视频显示「封面不可用」?** 那些源文件本身损坏(典型报错 `moov atom not found`,多为上游写入中断的 mp4)。抽帧失败会留 `.fail` 标记不再重试;**源文件修好后**用 `POST /api/thumbs {"purge":true}` 清标记再生成即可。


## MCP 接入(阶段二,零依赖 stdio server)

`mcp_server.py` 让 AI 助手(Claude Desktop / CodeBuddy 等)通过 MCP 直接检索素材库。

注册(配置文件里加):
```json
{"mcpServers": {"materials-hub": {
  "command": "python",
  "args": ["d:/MineWeb/2026/Vitual/materials_hub/mcp_server.py"]}}}
```

工具:`search_materials(q,kind?,tag?,mode?,limit?)` 中文自然语言检索 /
`get_material(id)` / `list_tags(limit?)` / `hub_stats()`。
启动时会顺手自动拉起 ollama(失败不影响词法检索)。日志走 stderr,协议走 stdout。
