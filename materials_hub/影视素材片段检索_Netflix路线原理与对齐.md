# 影视素材片段检索：Netflix 路线实现原理（与本项目对齐）

> 本文档是「原理性参考」，讲清行业（Netflix 这类影视素材检索）为什么这么做、整体流水线长什么样。
> 具体操作落地见既有文档：检索/RAG 做法见 `素材中心最佳实践与优化分析.md` §C；画面内容检索见 `画面内容检索落地方案.md`；受控词表见 `tag_vocab.py` 与 autotags 流程；回归门禁见 `eval_search.py` 的 `GATE_BASELINE`。
> 末尾 §6 把每条 Netflix 路线组件映射到本仓库（materials_hub / subtitle_pipeline）的现状，标 ✅已做 / 🟡部分 / ❌缺口。

---

## 0. 对齐依据（最佳实践来源）

| 来源 | 关键结论 | 本项目对应 |
|---|---|---|
| Netflix 影视数据中台 / 行业共识 | 不逐帧向量化；先切片 → 多模态并行 → 混合召回 → 重排，向量只是一路信号 | 本仓库整体路线（见 §6） |
| TransNetV2 / ffmpeg scenecut | 镜头 = 检索单元，1~4 关键帧/镜头 | `build_shot_index`（ffmpeg 场景切分） |
| CLIP / SigLIP / Marengo / InternVideo | 视觉语义向量（补充信号） | `clip_runner`(SigLIP2-256) + `clip_frame_embeddings` |
| bge-reranker / cross-encoder | 召回后精排 | `rerank_runner`(bge-reranker-v2-m3 ONNX) |
| Milvus / FAISS / OpenSearch KNN | ANN 近似索引支撑亿级向量 | 当前 SQLite + 本地文件（规模未到，预留） |
| 受控词表（DAM 最佳实践） | 结构化实体标签优于自由文本 | `tag_vocab.py`(~168 对) + autotags 互验 |
| 内部既有最佳实践文档 | 混合检索 / 门禁 / sidecar 铁律 | `素材中心最佳实践与优化分析.md` / `画面内容检索落地方案.md` |

---

## 1. 一句话核心结论

行业（Netflix 这类影视素材检索）**绝对不会对几十万部影片的每一帧全部向量化**——视频连续帧冗余极高。整套是「**先切片 → 多模态并行提取 → 混合召回 → 重排精定位**」的流水线；**向量只是其中一路信号，不是全部**。

---

## 2. 入库预处理：怎么把一整部电影切成可检索单元

> 解决"全帧向量爆炸"：把"帧"这个过细的检索单元，换成"镜头/片段"。

### 2.1 镜头/场景分割（Shot Boundary，TransNetV2 这类模型）
- 一部 100 分钟电影大概是 **几百～一千多个镜头**，而不是 144000 帧（24fps）。
- 同一个镜头内画面几乎不变，**不需要逐帧编码**。
- 硬切、淡入淡出识别，把视频切为独立镜头片段（**2s~10s 为主**）。
- 每个镜头里面，只采样少量**关键帧（1~4 帧/镜头）**；也可以直接输出这个镜头整体的**时空视频向量**（TwelveLabs Marengo、InternVideo）——一个镜头 1 条向量，进一步压缩数量。
- 举例：一部电影从十几万帧 → 产出几百条向量，**压缩几百倍**。

### 2.2 多路并行提取，不止画面向量（Netflix 标准做法）
同一个镜头时间轴上，并行跑多套模型，产出**多索引**，不是只存视觉向量：

| 信号 | 产出 | 用途 |
|---|---|---|
| ASR 语音转字幕 + 剧本对齐 | 台词文本向量 | **最常用、召回最快** |
| OCR | 画面内文字（海报、车牌、标题） | 文字检索 |
| 人脸识别库 | 演员身份向量（单独人脸索引） | 找特定演员 |
| 物体/动作检测 | 枪、汽车、爆炸、握手等实体标签 | 结构化倒排索引 |
| 视觉 Embedding | CLIP / SigLIP / 专用 Video Encoder 画面语义向量 | 语义检索 |
| 人工/剧本元数据 | 片名、年代、导演、场景描述、剧情摘要 | 分面过滤 |

**所有信息带上统一时间戳（start/end），绑定到同一个镜头 ID。**

### 2.3 向量本身的压缩优化
- **精度量化**：FP32 → FP16 / INT8，体积直接缩小 2~4 倍，对相似度损失很小。
- **ANN 近似索引（HNSW）**：向量库不是暴力全量比对，是近似最近邻搜索，支持千万/亿级向量秒级召回。
- **冷热分层**：热门素材向量常驻内存；老片冷素材向量存在对象存储，检索时按需加载。

---

## 3. 检索链路：用户输入一句描述，是怎么找到片段的

Netflix 内部素材检索是**两级召回 + 重排**，不是只靠向量。

### 3.1 第一级：粗筛（低成本过滤，大幅减少候选池）
先用**文本倒排索引**（字幕、剧本、实体标签）做关键词 / 文本向量召回，先把几十万部电影筛到**几十/几百个候选镜头**。

> 比如搜索"雨夜男主独自走在街道"，优先匹配字幕、剧情标签，**先砍掉 99% 无关素材**；向量比对只在小候选集里面跑，而不是几十万部全量向量比对。

### 3.2 第二级：多模态向量精召回
用户自然描述转 Text Embedding，和候选集里面的**镜头视觉向量、字幕向量**做相似度匹配。

### 3.3 时序重排 + 局部精定位
召回得到候选镜头之后，**VLM（多模态大模型）再读取这个镜头内的关键帧 + 字幕**，判断是不是真的符合描述；
如果需要更精确时间点，再在这个小段内做**细粒度采样**，而不是全片扫描。

---

## 4. 数据量直观对比

假设：几十万部影片，平均每部 100 分钟，24fps。

- **如果每一帧都存向量**：单部 ≈ 144000 向量，几十万部就是**千亿级别**向量，存储、GPU 推理、检索延迟完全不可行。
- **行业方案（镜头 + 关键帧）**：单部 ≈ **500~1000 条向量**，几十万部最终是**亿级向量**，配合量化、分片向量库（Milvus / FAISS / OpenSearch KNN），工程上可控。

---

## 5. 两种主流路线区分（对应 Netflix 影视数据中台）

1. **Netflix 内部制片素材库路线（面向剪辑、营销找镜头）**
   强依赖【剧本-字幕-镜头对齐 + 人脸/实体知识库 + 视觉向量混合检索】，优先保证"找特定演员、特定台词场景"，**视觉向量是补充，不是主力**。

2. **通用商用素材平台路线（Pexels、Shutterstock AI 检索）**
   更多直接用**固定时间切片**（比如 4s / 10s 一个视频向量），单条向量表征一小段画面动作，适合纯视觉描述检索。

---

## 6. 局限（为什么不能只靠向量）

- **纯视觉向量容易语义漂移**："一个人举枪"和"一个人举麦克风"相似度可能很高。
- **快速一闪而过的短动作**：镜头级向量会被平均掉，需要调高采样密度、增加重排。
- **成熟系统永远是**：文本（字幕剧本）优先召回，视觉向量做语义打分，实体标签做约束过滤。

---

## 7. 本项目对齐表（materials_hub / subtitle_pipeline）

| Netflix 路线组件 | 本项目落点 | 状态 | 缺口 / 下一步 | 参考文档 |
|---|---|---|---|---|
| 镜头/场景分割（检索单元=镜头，1~4 关键帧） | `build_shot_index(mid, max_scenes=60)` + MCP `hub://shots`（ffmpeg scenecut 镜头表 `index/shots/<id>.json`，含 start/end）；`auto_process_material` 链含 shots | 🟡部分 | 现用 ffmpeg 场景切分，非学习式 TransNetV2；尚未把"镜头"作为独立召回单元做镜头级向量 + 时序精定位 | 画面内容检索落地方案 §3.8 |
| ASR 字幕 + 剧本对齐（主召回信号） | `ASR_DIR` + `asr_sidecar_path` + `_ASR_FIELD_WEIGHT=0.35`（独立加权通道，与 OCR/visual 同构）；字幕由 subtitle_pipeline 同 job 挂载 | ✅已做(字幕) | 剧本对齐无；ASR 来自 SP bridge（只读引用），非本库内生成 | 素材中心最佳实践与优化分析 §C |
| OCR 画面内文字 | `ocr_runner.py` → `index/ocr/<id>.txt` sidecar，`_OCR_FIELD_WEIGHT=0.35`，绝不写 `description` | ✅已做 | — | 画面内容检索落地方案 |
| 人脸识别库（演员身份向量，单独索引） | 无 | ❌缺口 | 需独立人脸索引 + 演员知识库（当前靠 VLM 描述/标签近似） | — |
| 物体/动作检测（实体标签→结构化倒排） | `tag_vocab.py`（~168 个 (中,英) 受控对）+ `build_auto_tags`：SigLIP2 zero-shot 余弦过阈挑选 + `index/autotags/<id>.json`（VLM∩SigLIP 互验 verified/auto） | ✅已落地 | 已建 `build_entity_inverted_index` 结构化实体倒排(`index/entity/entity_inverted.json`),`entity_search(op=and|or)` 支持多实体组合约束(`cli.py entity-index`/`entity-search`);非检测框,但「实体标签做约束过滤」已落地(真实库实测 38 实体/23 素材) | — |
| 视觉 Embedding（CLIP/SigLIP/Video Encoder） | `clip_runner.py`(SigLIP2-256/webli，2026-10-10 替换 ViT-B-32) + `clip_frame_embeddings` 多帧向量；`build_clip_frame_embeddings` | ✅已做 | Video Encoder（时序聚合，如 InternVideo/Marengo）未做；CLIP 文本→垂类弱，仅补充 | 画面内容检索落地方案 §2B |
| 人工/剧本元数据 | `fetch_meta.json` 标题/描述、受控 `sp\|...` 标签、name/description | ✅已做(基础) | 剧情摘要/年代/导演等结构化元数据稀疏 | — |
| 统一时间戳绑定 shot ID | shot 表含 start/end；OCR/visual/asr/autotags sidecar 按 id 落盘（逻辑切片，不落盘） | 🟡部分 | OCR/visual/autotags sidecar 未显式带 (start,end) 时间轴绑定到镜头 | — |
| 向量压缩（量化/ANN/HNSW/冷热） | SQLite 索引 + 本地文件；规模小（~127 条）无需 ANN | ❌缺口(规模未到) | 规模化时引入 FP16/INT8 量化 + HNSW（FAISS/Milvus/OpenSearch KNN）+ 冷热分层 | — |
| 第一级粗筛（文本倒排先砍 99%） | 词法检索（门禁 `GATE_BASELINE` p5 .78 / r20 .92 / mrr .93 / ndcg .92，容差 .02）为主召回；字幕/描述/标签优先 | ✅已做(理念一致) | — | 素材中心最佳实践与优化分析 §C / eval_search.py |
| 第二级多模态向量精召回 | 语义 bge-m3 稠密 + CLIP 文本→帧；RRF 融合 | ✅已做 | — | — |
| 时序重排 + VLM 局部精定位 | `rerank_runner.py`(bge-reranker-v2-m3 ONNX cross-encoder) 默认 auto；VLM(gemma4:e2b) 出描述/标签 | 🟡部分 | rerank 已落地；"读镜头关键帧+字幕判定并精确定位时间点"未显式做成检索步骤 | — |
| 局限：纯向量语义漂移/短动作被平均 | 文档已载明 CLIP 文本→垂类弱，主用 VLM 描述；门禁守护不退化 | ✅已对齐 | — | 画面内容检索落地方案 §4 |

### 对齐结论
- 本项目**已经自发贴合了 Netflix 路线的主干**：①混合检索（文本倒排粗筛 + 多模态向量精召回 + rerank）；②视觉向量只当一路补充信号（主用 VLM 描述 + 词法）；③OCR/视觉/ASR/实体标签走 sidecar + 低权重通道，绝不污染主排序 `description`（门禁守护）。
- **主要差距**（与规模/数据有关，非路线错误）：
  1. 检索单元仍是"素材"而非"镜头"——`build_shot_index` 已产镜头表，但还没把镜头作为独立召回单元做镜头级向量与时序精定位；
  2. 缺独立**人脸索引 / 演员知识库**（当前靠 VLM 描述/标签近似）；**检测式实体倒排已落地**（`build_entity_inverted_index` 把 autotags 实体建成可组合倒排，`entity_search` 支持 AND/OR 组合约束）—— 见 §8 P1-1；
  3. 缺**剧本对齐**（ASR 字幕已有，来自 subtitle_pipeline 桥接引用）；
  4. 规模未到，**量化 + ANN(HNSW) + 冷热分层**暂不需要，但应作为规模化路线预留。

---

## 8. 优化路线图（按 P0/P1/P2，对齐《素材中心最佳实践与优化分析》）

> §7 已列缺口。下面按"性价比 + 与现有能力的距离"排序，复用既有 best-practice 文档的 P0/P1/P2 框架。每项标【现状 → 动作 → 落点】，落点均指向已验证的真实模块，不凭空造能力。

### P0（低成本高收益，现有能力可拼，无需新模型）
1. **镜头级时间定位先可用**
   - 现状：`core.get_shots(mid)` / MCP `hub://shots/{id}` 已能按镜头 start/end 做**逻辑切片**（不落盘）。
   - 动作：检索命中素材后，在 UI/API 层用已有 shot index + VLM 画面描述回带 `start/end`，让用户直接跳到相关镜头。
   - 落点：纯编排层工作，无需新向量；参考 `mcp_server.py` 的 `hub://shots` 用法。
2. **画面/视觉类查询补人工 GT 固化门禁** ✅ 已落地（2026-10-10）
   - 现状：`画面内容检索落地方案.md §7` 已列为后续；lexical/auto 门禁（`eval_search.py` `GATE_BASELINE` / `AUTO_GATE_BASELINE`）已能防回归。
   - 动作：把"排骨"/"红烧肉"等画面词的相关 id 集固化进 `eval_ground_truth.json`，使门禁能量化验证画面召回。
   - 落点：`eval_ground_truth.json` 新增 `红烧肉` 查询（4 条匹配资产：76624260137a/896b9f074ad6/d85eb3f4b42f/9ce0ad1442b8），门禁仍 PASS（p5 .78 / r20 .92 / mrr .93 / ndcg .92）。
3. **时序重排显式化**
   - 现状：`rerank_runner.py`（bge-reranker-v2-m3 ONNX）已默认 auto；VLM（`gemma4:e2b`）已能出画面描述。
   - 动作：在召回候选上叠加一步 VLM 判定（读镜头关键帧 + 字幕），输出命中镜头 + 置信度，做成独立检索步骤。
   - 落点：`rerank_runner` + `visual_runner` 已有能力编排。

### P1（中投入，显著扩展召回维度）
1. **受控词表 → 结构化实体倒排** ✅ 已落地（2026-10-10）
   - 现状：`index/autotags/<id>.json`（verified / auto，来自 `tag_vocab.py` ~168 对 + SigLIP2 + VLM∩SigLIP 互验）已是实体标签。
   - 动作：把这些标签建成**可组合的结构化倒排**（如"红烧鸡翅 + 鸡翅"AND 约束），而非仅词法 bigram 命中。
   - 落点：`core.build_entity_inverted_index()` + `core.entity_search(op=and|or)`；CLI `entity-index` / `entity-search`；真实库实测 38 实体 / 23 素材。
2. **剧本 / 字幕时间轴对齐**
   - 现状：ASR 字幕已进 `ASR_DIR`（`asr_sidecar_path`，同 job 挂载，`_ASR_FIELD_WEIGHT=0.35`）；缺的是按时间轴绑定到素材并支持"台词 → 时间点"跳转。
   - 动作：复用 shot index 的时间轴把字幕切片对齐，支持台词级跳转。
   - 落点：`build_shot_index` 时间轴 + `ASR_DIR`。
3. **视觉向量补 Video Encoder 时序聚合（可选）**
   - 现状：`clip_frame_embeddings` 是每帧独立向量（`build_clip_frame_embeddings`）。
   - 动作：对长动作加 InternVideo / Marengo 式镜头级时空向量，缓解"短动作被平均"。
   - 落点：`clip_runner.py` 扩展。

### P2（规模化才需要，当前 ~127 条不必做）
1. **向量压缩 + ANN**
   - 现状：SQLite 索引 + 本地文件，规模小无需 ANN。
   - 动作：FP16/INT8 量化 + HNSW（FAISS / Milvus / OpenSearch KNN）+ 冷热分层；规模到万级素材/千万级向量时引入。
   - 落点：检索后端替换。
2. **独立人脸索引 + 演员知识库**
   - 现状：靠 VLM 描述/标签近似找人，无独立人脸 embedding。
   - 动作：建人脸 embedding 索引 + 演员名↔identity 知识库，支撑"找特定演员"。
   - 落点：新增识别管线。
3. **全量剧本结构化元数据**
   - 现状：片名/年代/导演/剧情摘要稀疏（靠 `fetch_meta.json`）。
   - 动作：接入更完整的媒资元数据，支撑分面过滤与剧本对齐。
   - 落点：元数据层扩展。

> 优先级判断原则（与 Netflix 路线一致）：**文本（字幕/剧本/实体标签）召回永远优先，视觉向量做语义打分，实体标签做约束过滤**；P0 不引入新模型即可把"找镜头"体验补齐，是性价比最高的下一步。
