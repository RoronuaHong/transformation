# 决策模型（Jev / System One 范式）调研与落地评估

> 调研日期：2026-10-11。定位：先写文档、后谈代码。结论面向 materials_hub / kb / subtitle_pipeline 三个子项目。
> 阅读顺序：§1 结论速览 → §2-3 赛道与原理 → §4 官方最佳实践 → §5 本项目接入点逐个过 → §6 硬件可行性 → §7 落地路线 → §8 风险与不做清单。

---

## 1. 结论速览（TL;DR）

| 问题 | 结论 |
|---|---|
| Jev 是什么 | TypeSafe AI（2026-09-15 发布）的**非文本决策模型**：不生成一个字，只输出 choice / score / noul 三原语 + 校准置信度。发布 21 天日处理 1 万亿 token，a16z 领投估值 75 亿美元 |
| 能不能直接接入本项目 | **不能**。Jev 是云端 API（`api.typesafe.ai`，`TYPESAFE_API_KEY`），与本项目"全离线"铁律冲突 |
| 有没有离线路径 | **有，且协议兼容**。llama.cpp 已原生合入 `/v1/systemone` 端点（PR #29818/#29831），Cloudflare 开源的 **Clef / Clef-flash（Apache 2.0，可商用）** 提供官方 GGUF（含打分头）。同一套 SystemOne 客户端代码可零改动从 Jev 云端切到本地 llama-server |
| 最值得试点的接入点 | ① `_query_intent`（core.py:1096，纯关键词规则，未见过的问法就漏）② autotag 单方证据（auto）条目的仲裁。两者都是**批量、离线、延迟不敏感**的判断类任务 |
| 明确不碰 | subtitle_pipeline `classify_dialogue_route` 的 `flat_band_glyph_px` 像素铁律（可解释、有门禁守护、已证死 ML 路线不划算）；agent `confirm=true→allow_write` 写护栏（决策模型只做辅助参考，绝不替代人工确认） |
| 第一动作 | P0：本地起 `llama-server + Clef-Flash-GGUF` 冒烟 `/v1/systemone`，再对 `_query_intent` 做带 GT 的替换实验（门禁不回归才准换） |

---

## 2. Jev 与 System One 范式

### 2.1 三原语（Jev 协议，开源模型沿袭）

| 原语 | 用途 | 返回 |
|---|---|---|
| `choice` | 从命名选项集中选一个 | 所选项 + **每个选项的概率** + confidence |
| `score` | 按有序描述性等级打分 | 各等级概率 + **期望分数（如 1.59）** + confidence |
| `noul` | 是/否判断 | 为真的概率（0~1）+ confidence |

交互模式：`state（结构化上下文）+ typed questions → 结构化答案`。**没有文本生成**，`output_tokens` 恒为 0。
进阶：指令、选项、等级、标准都可传 JSON 结构（Advanced structure）。

### 2.2 关键设计概念

- **State**：结构化状态作为判断上下文；官方实操指南强调**必须配套证据信息**，缺佐证材料的判断参考价值大打折扣。
- **Confidence 与 probability 是两个轴**：probability 回答"是什么"，confidence 回答"该不该执行"——Confidence-gated routing 的基础。注意：官方承认概率按模型内置温度缩放，**在具体业务数据上未必校准过，门控阈值需自行标定**（见 §4.3 工程坑）。
- **模型 jaggedness**：TypeSafe 官方公开 `jev-1.13` 的已知不完美（jaggedness 页），说明这一代决策模型仍处早期。
- 当前版本 `jev-latest`（1.13），仅 32k 上下文、仅文本输入；闭源 API，价格未公开（批量并行提问"便宜 12.2x"是相对值，非绝对价）。

### 2.3 与 LLM 的分工

决策模型在知识推理类基准（GPQA/BBH/MMLU-Pro）弱于大 LLM——它**只做判断、不做表达**。官方 AI primer 的解释：训练目标是**校准概率的决策模型**而非生成模型。对应 CSDN 实测对照：同一 state 用"生成模式 + json_object"能解决格式问题，但**解决不了判断可靠性**（urgency 判 0、outage 判 false 全错），而 `/v1/systemone` 原生决策 64~92ms 全对——这正是决策模型存在的意义。

---

## 3. 赛道生态（2026-10 现状）

| 产品 | 发布方 | 规格 | 协议 | 接口 | 备注 |
|---|---|---|---|---|---|
| **Jev** | TypeSafe AI | 闭源 | — | SystemOne 原生（云 API / Python / JS SDK） | 意图分类 BANKING77 仅 79.7，中位延迟 524ms（云端） |
| **Clef / Clef-flash** | Cloudflare（2026-10-01） | 27B / 9B | **Apache 2.0（可商用）** | 兼容 Jev/SystemOne；官方 ggml-org GGUF | Qwen3.5 主干（64 层混合注意力：48 线性 + 16 全注意力，256K 上下文）+ LoRA + 打分头；**支持图片/视频输入**；Clef-flash 中位延迟 38.8ms |
| StartLux-Decision | StartLux（原点星辉） | 0.8B/2B/4B/9B/27B | **CC BY-NC 4.0（仅非商业）** | 兼容 /v1/systemone | 自测 Decision Index 63.88 |
| Intern-Decision | 上海 AI 实验室 | 4B 等 | 见其仓库 | — | — |
| OpenAI Decisions API | OpenAI（2026-10-06） | 闭源 | — | — | Jev 发布三周后的官方跟进 |
| Clef RL 平台 | Cloudflare | — | — | Workers AI | 用自有数据微调决策模型 |

**基准口径警告**：Clef 的"Jev Decision Index 第一"来自 Cloudflare **内部评测**，StartLux 的高分是**自测**——各家套件版本、快照时间不同，**严格说不能直接比大小**。方向性结论一致：开源决策模型已整体越过 Jev 这条线；Clef-flash 在多个决策类基准（API-Bank 93.1、ContractNLI 84.3、ForecastBench Brier 10.6）领先，延迟仅为 Jev 的 7%。

**llama.cpp 支持时间线**：PR #29818（10-02 合入）原生 `/v1/systemone` 端点，首批支持 5 个决策模型；PR #29831（次日）加入 Clef，打分头直接进计算图。官方预量化 GGUF 三档（BF16/Q8_0/Q4_K_M）。

---

## 4. 官方最佳实践（TypeSafe patterns/cookbooks 提炼）

### 4.1 设计哲学：窄决策

> 让代码保持控制权，只给 System One **窄而结构化的决策**。

- **问题原子化，组合逻辑放代码里**：一次只问一个可独立评估的判断；组合推理仍由代码完成。
- **并行提问（fan-out）**：一次调用发多个问题，批量 13 问比逐问便宜 12.2 倍、快 10 倍——因为决策是非自回归的，加问题几乎不增延迟。
- **必须给证据**：state 里放结构化事实（来源 + 值 + 置信度），不要只丢一段裸文本。
- **动态更新选项清单**（借鉴 Browser Use）：失效的旧选项要剔除，避免干扰判断。

### 4.2 官方 patterns 一览（与本项目对应）

| Pattern | 官方cookbook | 本项目对应 |
|---|---|---|
| Intent routing | 分类请求→路由到确定性逻辑/专家LLM/人工 | `_query_intent`（见 §5.1） |
| Confidence-gated routing | 答案给"是什么"，confidence 给"做不做" | autotag verified/auto 两级证据（§5.2） |
| Speculative fan-out | 并行问 | autotag 批量仲裁可一次问多条素材 |
| SDE cascade | mini 抽取→验证→推理两阶段 | 暂无对应；若做 KB 答案核查可借鉴 |
| RAG rerank（法律场景 top-1 5%→18%） | 决策模型做重排 | 已有 bge-reranker-v2-m3，边际小（§5.3） |
| LLM 护栏 / 引用核查 | 屏蔽/审核/放行；答案是否被来源支撑 | KB `/ask` 防幻觉（§5.4） |

### 4.3 工程坑（来自 Clef 本地实测，接本地必读）

| 坑 | 说明 |
|---|---|
| **必须用官方 ggml-org GGUF** | bartowski 社区量化缺 160 个打分头张量与 `clef.decision.type` 元数据，`/v1/systemone` 返回 501，无法补救 |
| **`-ub` 必须调大** | Clef 要求整个 prompt 在一个物理 batch 算完；默认 `-ub 512` 时约 980 token 即报 HTTP 500。建议 `-b 4096 -ub 4096` |
| 仅 `/v1/systemone` 端点 | chat completions 不可用；视觉输入需等 llama.cpp PR #29622 |
| 单 batch 单序列 | 首请求约 500ms（计算图编译一次性开销） |
| 速度只受 prompt token 数与算力影响 | 决策耗时几乎全是 prefill 前向传播；`--flash-attn`/`-ngl` 等怎么调都差不多，新版 `--fit` 默认自动调参 |
| **门控阈值必须自标定** | 官方承认概率按内置温度缩放，业务数据上未必校准——接入前先在自有 GT 上画 reliability 曲线定阈值 |

### 4.4 本地实测协议（本仓库实测确认，2026-10-12）

> 以下为 **本机 llama-server + Clef-Flash-Q4_K_M 实测反推**（官方文档无此细节，靠 400 报错逐字段试出），写代码以本节为准。

端点：`POST http://127.0.0.1:8911/v1/systemone`，所有响应 `usage.output_tokens = 0`（确认非生成式）。

**请求骨架**——注意 `questions` 是**对象**（按问题 id 映射），字段名是 `instructions`（复数）：

```json
{ "state": "用户查询：...", "questions": { "<id>": { "type": "...", "instructions": "...", "criteria": ... } } }
```

| 原语 | `criteria` 形态 | 响应 |
|---|---|---|
| `choice` | **对象** `{"菜品":"画面主体是做好的食物","人物":...,"风景":...}` | `{choice, probabilities{各选项}, confidence}` |
| `noul` | **对象** `{"true":"...","false":"..."}` | `{noul: 0.973}`（为真的概率） |
| `score` | **数组 2~10 级** `["很模糊","一般","很清晰"]` | `{score: 1.88(期望级), legend, probabilities, confidence}` |

**踩过的字段名坑**（报错原文 → 正确写法）：
- `"questions" must be a non-empty object` → 不是数组
- `questions.<id>: "instructions" must be provided` → 复数，不是 `instruction`
- `questions.<id>: "criteria" must be a non-empty object` → choice/noul 用对象
- `questions.<id>: "criteria" must be an array of 2 to 10 levels` → 仅 score 用数组

**实测结果**（RTX 2060 6GB，权重 6.49GB 部分 offload）：

| 用例 | 结果 |
|---|---|
| `_query_intent` 语义三分类（id/speech/text，6 条样例） | **6/6 命中**，概率 0.955~0.992，confidence 0.93~0.99 |
| 单次请求延迟 | **约 1.7s**（prefill-bound，权重超出 6GB 显存只能部分 offload） |
| noul 判定（素材是否食物类） | 概率 0.973，正确 |
| score 打分（画面清晰度 3 级） | 期望级 1.88（倾向"很清晰"），confidence 0.82 |

判定质量达标；**延迟 1.7s 是本地部署的主要代价**，因此接入策略必须是"规则优先、模型兜底/离线批量"，不能放到交互式主链路的同步关键路径上。

---

## 5. 本项目接入点逐个过

> 原则：文本（字幕/剧本/实体）召回优先、视觉向量做语义打分、**实体标签做约束过滤**——决策模型只进入"判断"环节，绝不进入检索主链路（不改变 `search()` 主排序，避免动检索门禁基线）。

### 5.1 `_query_intent` 决策化 —— P0 首选试点

```1096:1106:materials_hub/core.py
def _query_intent(q):
    """文件名、编号、type:/job: 走精确检索;问台词不拿画面向量补。"""
    s = (q or "").strip()
    low = s.lower()
    if low.startswith(("type:", "job:", "tag:", "id:")) or "\\" in s or "/" in s:
        return "id"
    if re.fullmatch(r"[0-9a-f]{8,}", low):
        return "id"
    if any(k in s for k in ("台词", "字幕", "说了", "听到", "这句话")):
        return "speech"
    return "text"
```

- **现状**：纯规则（前缀/正则/关键词表），3 类意图 id/speech/text。快、可解释，但**未见过的问法必漏**（如"他讲了啥"不在词表 → 误判 text 去拿画面向量）。
- **决策模型方案**：`choice` 原语，选项即 `id/speech/text`（可随路由扩展），state 放原始 query；**规则命中的走规则（确定性优先），规则未命中/低置信的再问模型**——规则仍是第一道门，模型只兜底。新增意图类别时只改选项清单，不改代码。
- **门控**：confidence 低于阈值 → 保守回退 `text`（现行为）。
- **验证**：现有 `eval_search.py --mode lexical --gate` 门禁不回归 + 新增意图 GT（把 8 个既有 GT 查询按意图标注 + 补漏句式）。

**✅ 已完成（2026-10-12，GT 实验结果）**

落点：`core.py` 新增 `decision_endpoint()` / `decision_choice_intent()` / `_query_intent_decided()`，测试 `tests/test_query_intent.py`。

| 通道 | GT 结果 | 耗时 |
|---|---|---|
| 规则通道（11 条） | 11/11 | 0ms |
| 规则+模型兜底（15 条，含 4 条规则漏判样本） | **15/15** | 约 1.4s/次 |
| 门禁 | pytest **204 passed**（14 条无服务时 skip）；检索门禁 **GATE PASS**（p5 .78/r20 .92/mrr .93/ndcg .92，零回归） | — |

关键结论与设计决定：
1. **默认关闭**：未设 `VITUAL_DECISION_URL` 时完全走规则（零网络、零延迟，`search()` 主链路不变），服务不可用时一律回落 `text`——服务挂了检索照常工作。
2. **不替换 `_query_intent`**：实测 1.4~1.7s 的延迟不适合交互式同步链路，因此保留规则为主、模型仅作离线/批量兜底与实验通道。
3. 模型确实接住了规则漏判：如裸文件名 `BK1234abcdef.mp4`（无分隔符、不匹配十六进制全串，规则判 text）被正确判为 `id`；`他讲了啥`（词表未收录）被判为 `speech`。
4. 门控：`VITUAL_DECISION_MIN_CONF`（默认 0.6）同时卡概率与 confidence；`VITUAL_DECISION_TIMEOUT`（默认 3s）防拖死。

### 5.2 autotag 单方证据（auto）条目仲裁 —— P1

- **现状**：`build_auto_tags`（core.py:4381）产出 verified=VLM∩SigLIP 互验 / auto=单方证据两级；auto 条目直接可检索但精准率无仲裁。
- **决策模型方案**：对 auto 条目用 `noul`/`score` 原语做第三方仲裁（state=素材描述+候选标签+SigLIP 分数），仲裁通过升级为 verified-adjacent，未过保持 auto。
- **优势场景**：批量离线、延迟不敏感、可用 fan-out 一次问多条；`index/autotags/<id>.json` 的 schema 只需加一个 `arbiter` 字段，不动主检索。
- **评估**：中价值中成本。依赖 §7 P0-1 的本地服务先跑通。

**✅ 已完成（2026-10-12，P1-1）**

`build_auto_tags`（core.py:4457）在产出 `auto` 单方条目后，若 `VITUAL_DECISION_URL` 已设，对每个素材**一次批量请求**（`decision_batch_noul`，单请求含多条 noul 命题）对所有 auto 标签做第三方仲裁，结果写进每条 `item["arbiter"]`（"pass"/"fail"），并在 sidecar 顶层加 `arbiter:"on"`。

| 设计点 | 实现 |
|---|---|
| 默认关闭 | 未设 `VITUAL_DECISION_URL` 时 `arbiter:"off"`，索引流程零变化、零延迟 |
| 零检索回归 | 仅**新增** `arbiter` 标注，不动 `verified`/`auto` 列表与三路检索权重 |
| 单请求多题 | 借用 llama-server 单请求可答多题的能力，一素材一次网络调用（~1.5s） |
| 阈值 | 复用 `VITUAL_DECISION_MIN_CONF`（默认 0.6）同时卡 noul 概率 |

映射逻辑抽成纯函数 `_arbiter_annotate` 已单测（`tests/test_query_intent.py`）。**注意**：promotion（pass→verified）未做，避免改变检索权重引发门禁回归——后续若要做，需先跑 `eval_search --gate` 确认。

### 5.3 检索重排 —— P2 观察

- **现状**：`semantic_rank`（core.py:2202）已有 bge-reranker-v2-m3 ONNX 重排，门禁基线 p5 .78/r20 .92。
- **对照**：官方 RAG rerank cookbook（法律场景 top-1 5%→18%）的前提是**没有专用 reranker**。本项目已有，边际收益存疑。
- **动作**：仅在 P0 服务稳定后做一次对照实验（同一 GT 上 reranker vs 决策模型 vs 级联），数据说话；默认不动。

### 5.4 KB 答案引用核查 —— P1（依赖 KB 离线跑通）

- **现状**：`kb/server.py` `/ask` 直接把检索 chunk 喂给 gemma4:e2b 生成，无引用核查——检索不准或生成幻觉时无法拦截。
- **决策模型方案**：生成后用 `noul` 判断"答案是否被 sources 支撑"（官方 citation-checking cookbook 同型），不支撑则降级为"仅返回检索原文"。
- **前置**：KB 先离线跑通（见 2026-10-11 日志：缺 `milvus-lite`，在线后 `pip install` + `build_index.py` 即可）。

**✅ 已完成（2026-10-12，P1-2）**

`kb/server.py` `/ask` 增加 `_decision_citation_supported()`：生成后若 `VITUAL_DECISION_URL` 已设，用 noul 判定回答是否被检索资料支撑；未支撑则**降级为仅返回检索原文**（附 ⚠️ 提示），响应新增 `citation_supported` 字段（默认 True，保持旧契约兼容）。

| 设计点 | 实现 |
|---|---|
| 默认关闭 | 未设 `VITUAL_DECISION_URL` 时 `citation_supported=True`，走原逻辑，零延迟 |
| 防幻觉 | noul 概率 < `VITUAL_DECISION_MIN_CONF`（默认 0.6）→ 降级，不让未支撑答案透出 |
| 实测 | 起决策服务后 `/ask` 验证：`citation_supported=True` 且回答正常（资产包+价格完整） |

这是**唯一直接面向用户生成内容**的接入点，价值最高（抗 KB 幻觉），且因默认关闭不影响现网。

### 5.5 Agent 写护栏 —— P2，只辅助不替代

- **现状**：`mcp_server.py:406` 写权限双重护栏：MCP `confirm=true`（人工复核）→ 才向 agent 传 `allow_write`。
- **决策模型方案**：在人工确认前用 `score` 给操作风险打分做**排序展示**（高风险排前面），绝不自动放行。confirm 人工铁律不变。

**✅ 已完成（2026-10-12，P2-2）**

`mcp_server.py` 新增 `decision_risk_score()`（score 原语：无风险/低/中/高四档），在 `t_agent_run` 响应里附带 `risk` 字段（score + confidence）——**仅辅助排序展示**，与 `confirm=true` 人工护栏正交，绝不替代它、绝不自动放行。默认关闭：`VITUAL_DECISION_URL` 未设时不带 `risk`。

### 5.6 明确不碰：subtitle_pipeline 路由铁律

`classify_dialogue_route` 的 `flat_band_glyph_px` 是像素级规则，已被 25.6 vs 7.7 的实测数据钉死为最优，且有路由契约测试守护。决策模型可解释性差、失败模式不可控，**替换它只增风险不增收益**。这条是反例：不是所有"判断"都该上模型。

### 5.7 把"Jev 决策模型"加进项目：落地总览

回答"jev 怎么加进去"——结论分两层：

1. **云端 Jev 本体不接入**：TypeSafe Jev 是 `api.typesafe.ai` 云端 API，与本项目"全离线"铁律冲突，直接排除。
2. **本地 Jev 等价体已加进去**：llama.cpp + Cloudflare Clef（`/v1/systemone` 协议与 Jev 同源）已在 `:8911` 跑通，同一套客户端零改动可从云端切本地。本项目通过它接入了 4 个判断类环节，**全部默认关闭、全部以 `VITUAL_DECISION_URL` 开关、全部零主链路回归**：

| 接入点 | 阶段 | 原语 | 开关 | 主链路影响 |
|---|---|---|---|---|
| `_query_intent` 意图识别 | P0-2 ✅ | choice | `VITUAL_DECISION_URL` | 仅规则兜底时问模型，search 主序不变 |
| autotag `auto` 条目仲裁 | P1-1 ✅ | noul(批量) | 同上 | 仅加 `arbiter` 字段，不改检索 |
| KB `/ask` 引用核查 | P1-2 ✅ | noul | 同上 | 未支撑→降级返回原文，默认关 |
| Agent 写风险排序展示 | P2-2 ✅ | score | 同上 | 仅响应附 `risk`，不自动放行 |
| 检索重排对照实验 | P2-1 ⏳ | — | 默认不动 | 已有 reranker，边际收益存疑，待 GT 对照 |

**怎么启用**（离线批量/分析场景）：
```
# 启动决策服务（start_suite 默认不拉，避免占显存抢 GPU 去字幕任务）
$env:VITUAL_START_DECISION='1'; .\start_suite.ps1
# 让各接入点开始问模型（服务关时一律回落原行为）
$env:VITUAL_DECISION_URL='http://127.0.0.1:8911'
```
**铁律**：延迟 1.4~1.7s，仅用于离线/批量/辅助展示，绝不进入交互式同步关键路径；服务不可用时所有接入点静默回落原逻辑。

### 5.6 明确不碰：subtitle_pipeline 路由铁律

`classify_dialogue_route` 的 `flat_band_glyph_px` 是像素级规则，已被 25.6 vs 7.7 的实测数据钉死为最优，且有路由契约测试守护。决策模型可解释性差、失败模式不可控，**替换它只增风险不增收益**。这条是反例：不是所有"判断"都该上模型。

---

## 6. 硬件与部署可行性（RTX 2060 6GB）

| 方案 | 显存 | 判断 | 速度预估 |
|---|---|---|---|
| Clef-Flash 9B Q4_K_M 全量 GPU offload | 6.49GB 权重（4070TiS 实测占 8.1GB） | **超 6GB，不可行** | — |
| Clef-Flash 9B Q4_K_M **部分 offload** | GPU 放 ~85% 层，其余 CPU | **可行（推荐）** | prefill-bound，估 0.3~2s/决策；离线批量完全够用，交互式意图识别也可接受 |
| StartLux-Decision 0.8B/2B | <2GB | 可行 | 快，但 **CC BY-NC 仅非商业** |
| Intern-Decision 4B | ~3GB | 可行 | 待验证其 GGUF 打分头支持 |

- **进程形态**：独立 `llama-server` 进程（类比 ollama :11434，建议 :8911），不要指望 ollama——其 llama.cpp 版本滞后，`/v1/systemone` 未必合入。
- **零依赖约束**：materials_hub 是纯标准库 → 用 `urllib.request` 调 `/v1/systemone`，**不引入任何新 pip 依赖**；服务不可用时降级回规则路径（try/except + 超时短平快）。
- **模型文件**：沿用 SP 的 `models/` 目录约定，GGUF 不入库（`.gitignore` 已有 `*.onnx`/`*.pth`，需确认补 `*.gguf`）。

---

## 7. 落地路线（每条：现状 → 动作 → 落点）

**P0（验证可行性，不动主链路）**
- P0-1 部署 llama-server + `ggml-org/Clef-Flash-GGUF`（Q4_K_M）→ 落点：独立进程 :8911，`-b 4096 -ub 4096`；冒烟 `/v1/systemone` 确认 choice/score/noul 三原语与 confidence 可用。
  **✅ 已完成（2026-10-12）**：`tools/llama.cpp/llama-server.exe`（b11429 CUDA 12.4）+ GGUF 已就位，三原语全部跑通，协议与实测数据见 §4.4。启动脚本 `tools/llama.cpp/start_llama.ps1`。
- P0-2 `_query_intent` 替换实验 → 落点：`core.py` 规则保底 + 模型兜底双通道；新增意图 GT；`eval_search.py --gate` 不回归才准合入。
  **✅ 已完成（2026-10-12）**：规则 11/11、规则+兜底 15/15，门禁零回归。**结论是不替换**——延迟 1.4~1.7s 不适合交互式链路，模型保留为 `VITUAL_DECISION_URL` 显式开启的离线/批量兜底通道（见 §5.1）。

**P1（价值落地）**
- P1-1 autotag auto 条目仲裁 → 落点：`build_auto_tags` + `index/autotags/<id>.json` 加 `arbiter` 字段；verified 通道不动。
  **✅ 已完成（2026-10-12）**：批量 noul 仲裁（单请求多题），`_arbiter_annotate` 单测通过；默认关、零检索回归（见 §5.2）。
- P1-2 KB 答案引用核查（noul）→ 落点：`kb/server.py` `/ask` 生成后校验；依赖 milvus-lite 安装。
  **✅ 已完成（2026-10-12）**：`citation_supported` 字段 + 未支撑降级返回原文；实测 `/ask` 验证通过（见 §5.4）。

**P2（数据说话才做）**
- P2-1 reranker vs 决策模型重排对照实验 → 落点：`semantic_rank` 只读对照，不改默认。**⏳ 待做**：已有 reranker，边际收益存疑，需同一 GT 上做对照实验再定。
- P2-2 agent 写护栏风险排序展示 → 落点：`mcp_server.py` confirm 流程辅助。
  **✅ 已完成（2026-10-12）**：`decision_risk_score()`（score 四档）+ `t_agent_run` 响应附 `risk`；只辅助排序，绝不自动放行（见 §5.5）。

**总览**：Jev 云端不接入；本地 Clef（`:8911`）已接入 4 个判断环节（P0-2/P1-1/P1-2/P2-2），全部默认关闭、全部 `VITUAL_DECISION_URL` 开关、全部零主链路回归。怎么启用见 §5.7。

**门禁原则（沿用仓库铁律）**：任何决策模型接入 = 新增 GT + 过 eval gate + pytest 全绿 + 前后肉眼对比；门控阈值必须在自有数据上标定（官方已警告未校准）。

---

## 8. 风险与明确不做

| 风险 | 应对 |
|---|---|
| 榜单口径混乱（自测 vs 内部评测） | 只信自有 GT 实验，不信宣传分数 |
| CC BY-NC 模型（StartLux）商用受限 | 商用场景只用 Apache 2.0 的 Clef 系 |
| 决策模型知识推理弱（GPQA/BBH/MMLU-Pro 落后 LLM） | 只用判断不用表达；表达类任务仍走 gemma4/qwen |
| 概率未在业务数据上校准 | P0 阶段先画 reliability 曲线定阈值，再谈门控 |
| 本地服务不可用导致主链路故障 | 一切接入都是"规则/现状保底 + 模型兜底增强"，服务挂了退回现状 |
| llama.cpp 视觉输入未合入（PR #29622） | 短期只做文本判断；画面类判断继续走 SigLIP2/VLM |

**明确不做**：
1. 云端 Jev API 直接接入（离线铁律）。
2. 替换 `classify_dialogue_route` / `flat_band_glyph_px` 像素路由（§5.6）。
3. 替代 agent `confirm=true` 人工确认（§5.5）。
4. 改 `search()` 主排序链路（检索门禁风险）。
5. 为决策模型引入新 pip 依赖（stdlib urllib 够用）。

---

## 附：信息来源

- TypeSafe 官方文档索引 `docs.typesafe.ai/llms.txt`（Primitives / Confidence / patterns / cookbooks / SDK）
- Cloudflare 官方博客：Introducing Clef（开源决策模型 + RL 微调平台）
- 《38毫秒：Cloudflare 开源的 Clef 决策模型，本地 6GB 就能跑》（CSDN 桑榆肖物，2026-10-09，含 llama.cpp 部署实测与基准对照）
- 36 氪/腾讯新闻/凤凰科技等对 Jev 发布、融资、竞品的报道（2026-10）
