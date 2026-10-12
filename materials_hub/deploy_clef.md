# 决策模型 Jev / SystemOne 接入与本地 Clef 部署

决策模型（SystemOne）用于 4 个环节的**可选**第三方复核/兜底，默认全部关闭
（不设 `VITUAL_DECISION_URL` 即退化为纯规则 / 纯 LLM，零延迟零回归）。

## 为什么默认关闭

- 本机服务以**交互式检索/字幕**为主，对延迟敏感；Jev 单次云端 3–12s、本地 ~1.7s。
- 本地 Clef-Flash 权重 **6.49GB 超出 6GB 显存**，只能部分 offload，更慢。
- 项目以**中文**为主，Jev 的 CJK 准确率低于英文（官方 jaggedness 已知），故规则优先 +
  中文场景提高置信门槛兜底。

结论：适合**离线/批量/异步**链路（入库理解、KB 回答后处理、agent 预评估），
**不挂交互式同步**。

## 两种接入方式

### A. 本地 Clef（延迟低，需错峰）

启动 llama.cpp `llama-server`，暴露 `/v1/systemone`（端口默认 8911）：

```powershell
# 示例：按实际 llama.cpp 版本调整参数；模型放 materials_hub/models/clef/*.gguf
llama-server -m models\clef\*.gguf --gpu-layers 20 --port 8911
```

然后设：

```powershell
$env:VITUAL_DECISION_URL = "http://127.0.0.1:8911"   # 不含 openrouter.ai 即走本地
```

**GPU 错峰（重要）**：6GB 卡被字幕去重 PM2 占用，直接起 Clef 会抢显存导致 OOM/互相拖慢。

- 方案 1：等字幕任务跑完（空闲时段）再启动 Clef；
- 方案 2：用 `nvidia-smi --query-gpu=memory.free --format=csv` 轮询，空闲显存足够才启动；
- 方案 3：指定不同物理 GPU（多卡时），或调低 `--gpu-layers` 让权重更多 offload 到 CPU/内存。

### B. 云端 OpenRouter（零本地显存，延迟高）

```powershell
$env:VITUAL_DECISION_URL = "https://openrouter.ai/api/v1"   # 含 openrouter.ai 即走云端
$env:VITUAL_DECISION_MODEL = "typesafe/jev-1.13"
$env:OPENROUTER_API_KEY = "sk-or-v1-..."
```

云端强制 timeout 下限 20s、默认重试 2 次（5xx/超时指数退避，4xx 不重试）。

## 开关与阈值（均可选）

| 变量 | 默认 | 说明 |
|---|---|---|
| `VITUAL_DECISION_URL` | 空 | **总开关**，空=全关 |
| `VITUAL_DECISION_MODEL` | `typesafe/jev-1.13` | 云端模型名 |
| `VITUAL_DECISION_TIMEOUT` | 本地 3s / 云端强制 20s | 单次超时 |
| `VITUAL_DECISION_MIN_CONF` | 0.6 | 放行门槛；低于=未支撑降级 / 仲裁 fail |
| `VITUAL_DECISION_HIGH` | 0.9 | 强支撑门槛；≥=strong/pass，[MIN,HIGH)=partial |
| `VITUAL_DECISION_MAX_SRC` | 5 | 引用核查最多取前 N 条资料 |
| `VITUAL_DECISION_MAX_CHARS` | 2000 | state 截断上限（长上下文防腐烂） |
| `VITUAL_DECISION_INTENT_HOT` | 关 | =1 时**热路径**也启用意图兜底（会加延迟，不推荐线上开） |

## 启用建议（哪些环节值得开）

| 环节 | 建议 | 理由 |
|---|---|---|
| P1-2 KB 引用核查 | **开**（离线/后处理） | 拦截 LLM 幻觉，降级返回原文，直接提升可信度 |
| P1-1 标签仲裁 | **开**（入库离线批量） | 对 SigLIP2 单方证据降噪 |
| P2-2 agent 风险排序 | **开**（写操作前） | 安全护栏，高危绝不自动放行 |
| P0-2 意图识别 | **保持规则** | 规则已用确定性前缀覆盖 id/speech，挂了反拖延迟 |

## 监控

```python
from materials_hub import core
print(core.decision_stats())
# {'calls':.., 'ok':.., 'err':.., 'other':.., 'dur_ms_max':.., 'avg_ms':.., 'ok_rate':.., 'other_rate':..}
```

`ok_rate` 低说明后端不稳（查 GPU 抢占/云端 5xx）；`other_rate` 高说明大量查询被归入
other（模糊/中文弱），可考虑抬高门槛或保持规则。
