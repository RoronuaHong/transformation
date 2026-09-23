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
- [联动 subtitle_pipeline](#联动-subtitle_pipeline快速上手)
- [运维:备份 / 重建 / 迁移](#运维备份--重建--迁移)
- [预览与缩略图策略](#预览与缩略图策略)
- [Python API](#python-apicore)
- [HTTP API](#http-apiserverpy)
- [已知限制与路线图](#已知限制与路线图)
- [FAQ](#faq)

## 功能(对应需求四件套)
- **目录规范**:固定结构,素材按类型归档。
- **自动采集整理**:SHA-256 去重、自动分类、命名规范化;可扫描 `ingest/` 或 `materials/` 全树,也可只读联动外部项目。
- **索引 + 检索**:SQLite 索引 + **零依赖加权语义近似检索**(中文按字 / bigram 分词、英数按词,字段加权打分)。自然语言问一句即可命中,不要求连续子串;支持按类型筛选、按标签过滤。
- **管理面板(Web/UI)**:浏览器可视化浏览、上传(含拖拽)、打标签、编辑描述、预览、删除、去重整理。

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

### 检索打分
字段权重:`name 3.0` > `tags 2.5` > `description 1.5` > `rel_path 1.0`。query 与各字段 token 重叠累计得分,按分排序;无 query 时按时间倒序。

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
└─ index/hub.db       # SQLite 索引库
```
> 仓库默认不入库 `*.md` / `*.html`;素材中心的文档与单页前端已在 `.gitignore` 末尾加 `!materials_hub/**` 白名单,可随代码一起版本化(参照 `kb/docs`、`box-lift` 先例)。`index/hub.db` 为生成物,不入库。

## 运行
需要 Python 3.12(避免 3.13 移除的 `cgi`;本服务已手写 multipart 解析,但建议 3.12)。

```bash
cd materials_hub
python server.py        # 启动面板,打开 http://localhost:8000
```
或纯命令行(见下方 CLI 参考)。

## Web 面板用法
- **搜索框**:自然语言 / 关键词,实时(输入即搜),按相关度排序。
- **类型下拉**:按 `kind` 筛选(图片/视频/文档/音频/其他)。
- **上传**:点「上传」或把文件**拖拽**到页面 → 落到 `ingest/` 并整理入库。
- **标签 / 描述**:每张卡片可编辑,点「保存」写回索引。
- **预览**:图片直接显示缩略图;视频内嵌播放器;音频 / 文档显示占位。
- **删除**:删内部素材会移入 `trash/`(不破坏原工程);删外部引用只删索引。
- **整理 / 扫描**:「整理 ingest/」处理待整理队列;「扫描 materials/」补录全树。
- **状态栏**:显示总数、重复组数、各 kind 数量。

## 检索示例
- 自然语言:`python cli.py search "bilibili 的足球比赛字幕"` → 命中标题含相关词、带 `bilibili` 标签的素材。
- 标签筛选:`job:BV1aDb56iEvu`(某 B 站视频全套素材)、`type:benchmark`(基准视频)、`type:render`(渲染预览)、`lang:zh`(中文字幕/笔记)。
- 组合:在面板里先选 `类型=视频`,再搜 `render` 或 `benchmark`。
- 空 query:直接按时间倒序列出(等同浏览全部)。

### 分面检索(Faceted Search)
成熟 DAM 的检索 = **关键词 + 多个维度同时过滤**。素材中心提供三个正交维度,可任意组合:
- **类型(kind)**:`images / videos / docs / audio / subs / other`(面板下拉)
- **标签(tag)**:`type:` / `job:` / `lang:` / `sp` 等(面板标签下拉,含计数)
- **时间(created_at)**:无 query 时默认按时间倒序
> 例:选 `类型=视频` + 标签 `type:benchmark` + 搜 `render` ⇒ 只列"基准视频里和渲染相关"的素材。维度越多越精。

## CLI 参考
```bash
python cli.py init                 # 初始化目录结构 + 数据库(幂等)
python cli.py ingest [path]        # 整理 path(默认 ingest/)下文件
python cli.py scan                 # 扫描 materials/ 全树补录索引
python cli.py search <关键词>      # 加权语义近似检索
python cli.py dupes                # 列出重复文件(SHA-256 相同)
python cli.py list                 # 列出全部素材
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
core.search(q="", kind="", tag="")  # 检索;q 为空按筛选+时间倒序
core.all_materials()                # 全部
core.get_material(mid)              # 单条
core.duplicates()                   # 重复组
core.update_tags(mid, tags)
core.update_description(mid, desc)
core.remove_material(mid)           # 内部移 trash,外部只删索引
```

## HTTP API(server.py)
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` · `/index.html` | 管理面板 |
| GET | `/api/list?q=&kind=&tag=` | 检索 / 列表 |
| GET | `/api/stats` | 总数 / 重复组 / 各 kind 数量 |
| GET | `/api/dupes` | 重复文件组 |
| GET | `/api/file/<id>` | 预览原文(外部引用读 `external_path`,支持 HTTP Range 流式) |
| POST | `/api/upload` | 上传(表单 multipart) |
| POST | `/api/ingest` | 整理 `ingest/` |
| POST | `/api/scan` | 扫描 `materials/` 全树 |
| POST | `/api/tag` | `{id, tags}` 更新标签 |
| POST | `/api/describe` | `{id, description}` 更新描述 |
| POST | `/api/remove` | `{id}` 删除 |

## 命名规范
非字母数字 / 中文 / 连字符统一为 `-`,折叠连续 `-`,截断至 80 字符 + 小写扩展名。原始文件名保留在 `orig_name` 字段。

## 运维:备份 / 重建 / 迁移
`index/hub.db` 是**派生索引(derived index)**,不是唯一真相源——真相是磁盘上的原文件。因此:
- **备份**:优先备份原始素材文件;`hub.db` 可随时重建,不必单独备份。需要可移植快照时用面板「导出 JSON / CSV」(`/api/export`)。
- **重建**:索引损坏或想重置,直接删 `index/hub.db`,然后
  `python bridge_subtitle.py`(重建外部引用)+ `python cli.py scan`(重建内部素材)。`sha256` 保证幂等,不会重复。
- **迁移**:把 `materials/` 与原项目一起拷贝,重跑上面的重建命令即可,无需迁移数据库。
> 这条"索引可丢弃、可重建"的原则,是本地素材库能长期稳定运维的关键。

## 预览与缩略图策略
- **图片**:直接返回,前端缩略图展示。
- **视频 / 音频**:`/api/file/<id>` 支持 **HTTP Range**,浏览器内嵌播放器可拖拽进度、边下边播(大文件也不整块读内存)。
- **视频缩略图(封面)**:当前**未生成**。本机未安装 `ffmpeg`,无法抽首帧。建议路线:
  1. 安装 ffmpeg → 新增 `/api/thumb/<id>`:对视频抽首帧 jpg 缓存到 `index/thumbs/`,前端作为 `<video poster>`;
  2. 或纯前端兜底:用 `<video>` + `canvas` 在加载时截首帧作封面(零服务端成本,但每个视频需加载一次)。
- **外部引用**(subtitle_pipeline 大视频)预览同样走 Range,不复制文件。

## 已知限制与路线图
**已知限制**
- **本地单用户**:服务监听 `0.0.0.0:8000` 但无认证/权限;仅本机或可信局域网使用,勿直接暴露公网。
- **无版本管理**:素材更新会覆盖索引记录,不保留历史版本。
- **非真·语义检索**:当前为加权关键词近似(见下)。
- **缩略图待 ffmpeg**:见上。

**后续可扩展**
- **真·语义 Embedding 检索**(可选增强):接本地 sentence-transformers / CLIP(图片),离线需预置权重;`search()` 已按分数排序,接入后替换 `_score` 即可(外部素材同样可语义搜)。
- **缩略图 / 转码**(见上,依赖 ffmpeg):首帧封面 + 可选转码代理以提速大文件浏览。
- **远程采集**:加网络抓取源(需明确站点与授权)。
- **多库隔离**:按项目分库。
- **MCP 推送**:由 subtitle_pipeline 的 `vitual_mcp` 把产物推送到素材中心(阶段二)。

## FAQ
- **为什么视频不复制进素材中心?** 体量大(subtitle_pipeline 下 177+ 个 mp4),复制会重复占盘。采用「原路径引用」,只登记索引,预览按需读取原文件。
- **删除素材会删掉原文件吗?** 不会。内部素材删除后移入 `trash/`(可找回);外部引用(subtitle_pipeline)删除只删索引,原文件毫发无损。
- **怎么重新登记 / 重建索引?** 删 `index/hub.db`,重跑 `python bridge_subtitle.py` + `python cli.py scan`(幂等)。
- **索引库坏了影响原文件吗?** 不影响。`hub.db` 是派生索引,可随时丢弃重建(见运维)。
- **能用自然语言搜吗?** 能。检索按字段加权做近似匹配,问句不必含连续子串;精确过滤用 `type:` / `job:` / `lang:` 标签。
