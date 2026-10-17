# materials-hub 工具 × 网络最佳实践 对齐审计（2026-10-16）

> 对照 2026 三域网络最佳实践，逐工具评估 aligned / partial / gap，并给优先级。
> 依据：RAG 2026 全栈选型（解析+分块+Embedding+向量库+**Reranker**+**混合检索**+生成）、
> Advanced RAG 2.0（混合检索+Rerank+**Recall 基线评估**）、医疗 RAG 实战（BM25+稠密混合）、
> AI Agents in 2026（**Tools/Memory/Evals/Guardrails** + tracing）、Agentic Observability 2026、
> DAM 最佳实践（集中元数据/受控词表/去重/多模态/治理/非破坏分发）。

图例：✅ 对齐 · ⚠️ 部分对齐 · ❌ 缺口

## 一、检索 / RAG
| 工具 | 最佳实践 | 现状 | 判定 |
|---|---|---|---|
| search_materials | 混合检索（BM25 稀疏 + 稠密向量） | lexical+semantic 混合 + 中英同义词 + 跨语言翻译桥 + kind/tag 过滤 | ✅ |
| search_materials | **Reranker 为核心阶段** | `auto` 默认走本地 bge-reranker-v2-m3 **ONNX cross-encoder**（已装可用）；ONNX 失败回退 ollama rerank（本次已加） | ✅ |
| search_materials | **Recall@k / MRR / nDCG 基线** | 有"检索门禁 GATE"，缺公开 recall 指标面板 | ⚠️ |
| chunk_search_materials | 父子分块（Parent-Child / Contextual） | 父子分块精准命中长文 | ✅ |
| search_by_image | 多模态检索（pHash + CLIP + 以文搜图） | phash/clip 双路 + text-to-image | ✅ |
| find_similar | 感知去重（dHash）+ 精确（sha256）互补 | dHash 近重复 + sha256 | ✅ |

**行动**：① **P1 已对齐**（auto 默认本地 bge-reranker ONNX；补 ONNX→ollama 回退）。② **P2 已实施** —— 新增 `search_eval` MCP 工具，按需跑 P@5/R@20/MRR/nDCG + 回归门禁（复用 `eval_search`；有 `eval_ground_truth.json` 用权威标注，否则启发式）。③ **门禁「假失败」已定位并修复** —— 实测 `p5=0.073` 远低于基线 `0.78`，诊断发现**语料已从 256+ 清到 19 条，而 GT 标注的 86 个 id 全部不存在于当前语料**（`coverage=0.0`，9/9 查询整组缺失）→ 召回虚低、门禁假失败，**不是检索回归**（检索对当前语料工作正常）。已加 `eval_search.gt_staleness()`（GT id 在语料中的存活率/覆盖度）、`search_eval` 的 `verdict` 字段（`gt_stale`/`pass`/`regression`），并让 CLI `--gate` 在 GT 过期时判 `GT_STALE (SKIPPED)` **退出码 0**（不再误报 FAIL/1）。实测门禁现输出 `0/86 存活、9/9 查询整组缺失 → SKIPPED, exit 0`。

## 二、多模态理解
| 工具 | 最佳实践 | 现状 | 判定 |
|---|---|---|---|
| run_ocr | 离线 OCR，文本入检索 | rapidocr 离线 + sidecar + 三路检索，不写 description | ✅ |
| run_visual | VLM 画面描述 | gemma4:e2b via ollama + sidecar | ✅ |
| build_image_embeddings | CLIP 图文向量 | open_clip + 权重 | ✅ |

**行动**：✅ **P3 已实施** —— `ocr_runner.py` 现输出每行 `text`+`box`(4点)+`score`；`ocr_material` 落结构化明细 sidecar `index/ocr/<id>.json`（与检索用 `.txt` 分离，不影响门禁），并回报 `low_conf_lines`（置信<0.7 待复核）。实测 23 行文字全带框+分（0.557–0.999）。

## 三、入库 / 元数据
| 工具 | 最佳实践 | 现状 | 判定 |
|---|---|---|---|
| register_asset | 受控登记 + 人工护栏 | 引用不复制、confirm | ✅ |
| auto_process | 幂等全链路管线 | 封面/OCR/镜头/pHash/语义，幂等，confirm | ✅ |
| update_tags | 受控词表、只增不删、系统标签保护 | 只增、系统面标签永不动、删须显式 remove | ✅ |

**行动**：✅ **P4 已实施** —— 新增 `near_duplicate_check()`（dHash 汉明距离，阈值 `VITUAL_INGEST_DEDUP_DIST` 默认 10）；`ingest_external` 新增素材时附带 `near_duplicates` 提示（**仅提示不阻断**，库内无 phash 时零误报）。实测重编码副本被检出 `dist=1`。

## 四、治理 / QA
hub_stats / governance_report / near_duplicate_report / list_missing_covers / distribution_readiness / job_checkup / job_lang_readiness —— 覆盖健康快照/治理扫描/近重复簇/缺封面/分发就绪/job 体检/语种就绪。**✅ 对齐**（覆盖面全）。
**行动**：✅ **P5 已实施** —— `governance_report` 新增 `suggestions` 字段，按问题类别给可执行动作链（只对 counts>0 的类别给建议，零问题类别不产噪声）。

## 五、Agent（Deep Agent）
| 工具 | 最佳实践 2026 | 现状 | 判定 |
|---|---|---|---|
| agent_run | 规划(拆待办)+工具编排+只读默认+防幻觉 | LLM 拆待办、只读默认、写需 confirm、seen_ids 核验剔除编造 | ✅ |
| agent_status | 可观测（步骤轨迹） | 待办/步骤轨迹/总结 | ✅ |
| agent_cancel / agent_resume | 协作取消 / 断点续跑 | 下一步边界取消、续跑复用待办+滚动摘要 | ✅ |
| agent_feedback / learning_summary | 反馈学习闭环 | 采纳/否决、采纳率、跨语言否决 | ✅ |
| assemble_package | 多智能体 + Critic 核验 + 非破坏 | Librarian→Critic→Executor→manifest，绝不改资产 | ✅ |

**行动**：✅ **P6+P7 已实施** —— P6：`_chat` 累计 ollama token/延迟 → `state["usage"]`（model/calls/prompt_tokens/completion_tokens/latency_s），`agent_status` 实时可见；P7：`agent_task_stats()` 扫 state.json 出 `success_rate`/`by_status`/`avg_steps`/`avg_usage`/`recent_failures`，并入 `learning_summary` 工具（已实测 `task_success` 上线；test_agent+test_obs 40 passed）。

## 六、打包 / 交付
assemble_package（manifest 驱动）+ deliver_package（转码/裁剪/格式归一、只新建不改原、confirm、流拷贝选项）。**✅ 对齐**。
**行动**：✅ **P8 已实施** —— `deliver_package` 的 `written`/`skipped` 产物均带 `sha256`+`bytes`（`_deliver_digest`，容错），供下游核对交付完整性；真实交付实测校验和正确，`test_deliver.py` 增回归测试（17 passed）。

## 优先级（"开始"入手顺序）
1. ~~P1 rerank 鲁棒化~~ **已对齐** — auto 默认本地 bge-reranker ONNX；补 ONNX→ollama 回退。
2. ~~P2 检索评估~~ **已实施**（`search_eval` 工具；全量 IDE 重启后出现在工具列表）。
3. ~~P6 Agent cost/token/latency 埋点~~ **已实施**（`_chat` 累计 + `state.usage` + `agent_status` surfaces）。
4. P3/P4/P5 均已完成（OCR bbox+置信 / 入库近重复预检 / 治理可执行建议）。

> 结论：工具套件主体已对齐 2026 最佳实践（混合检索 + cross-encoder rerank / 多模态 / 幂等 / 去重 / Agent 规划+HITL+可观测+可恢复+防幻觉 / 非破坏交付）。**P1–P8 增量已全部完成**（rerank 回退 / 检索评估+GT过期检测 / Agent 用量+成功率 / 交付 SHA256 / OCR bbox+置信 / 入库近重复预检 / 治理可执行建议 / 过期描述修复）。**门禁假失败已修**：GT 过期时判 `GT_STALE (SKIPPED)` 退出码 0，不再误报回归。剩余唯一实质项：**语料增长后用 `gen_ground_truth.py` 重标 GT**（届时 `search_eval` 的 `verdict` 自动转 `pass`/`regression`）。
