# 去硬字幕 / 去水印路线（dehardsub）

B 站与通用硬字幕的**默认路线（2026-11-24 起）**：**黄金标杆工作流**  
`dehardsub_gold_workflow.py`（`VITUAL_DEHARDSUB_GOLD_WORKFLOW=1`）。  
用户验收标杆：BV1P6 前 50 帧「非常完美」——见 [`eval/gold/dehardsub_gold_workflow/`](eval/gold/dehardsub_gold_workflow/README.md)。

**顶栏黄金锁（写进工作流，禁止「优化」跑偏）**：`wipe_top_watermarks` = regions **原框** + `ns_box_wipe(feather=6,radius=5)`；契约见 `top_gold_contract()`；回归见 `test_top_wipe_aligns_bv1p6_f0_to_gold_final2`。  
**禁止**调用前 `expand_static_overlay_box`（实测 Morphic MAD ~3.8→~9）、Morphic 贴片 / TELEA / ink 复合 / donor 贴顶栏。关键帧库只服务中景/对白。

旧 hybrid `ocr_telea` 多遍仅在 `GOLD_WORKFLOW=0` 时回退。引擎表仍为 `lama` | `ocr_telea`。**STTN / ProPainter 已下线**。

能力边界：**不能**承诺「任意视频 100%」；能承诺与黄金算法 1:1、框通用、失败可回退旧路径。

---

## 文档导读

| 章节 | 谁读 | 内容 |
|------|------|------|
| [优化工作流](#优化工作流目标态--必须按此执行) | 全员 | D0–D3 总图、现网 vs 目标、process 顺序 |
| [实现规格 D1–D7](#实现规格-d1d7目标态合同) | 改代码 | 阈值、行为、验收断言 |
| [产物与 meta 契约](#产物与-meta-契约) | 改代码 / 验收 | 文件布局、passes 必填字段 |
| [能力边界](#能力边界) | 产品 / 对外 | 可写 / 不可写文案 |
| [效果推演](#效果推演--查缺补漏bv1p6-demo-v13) | 排查 | v13 证据、safety 矛盾、spill、软 `@` |
| [验收协议](#验收协议) | QA | 命令、时刻表、门禁 |
| [落地优先级](#落地优先级) | 排期 | 0→6 + D8 起 |
| [外参第三轮查缺](#外参第三轮查缺补漏2026-11-17) | 排期 / 架构 | timeline、alpha、场景切、计划轨… |

---

## 优化工作流（目标态 · 必须按此执行）

一句话：**Detect 定角色 ROI → Confirm 框必须盖满墨 → Process 按角色分流且禁半抹 → QA 硬门禁决定 quality**。  
一视频只出一份 clean master；后续配音/新字幕吃这一份，禁止每语言重抹。

### 总图

```
source video
    │
    ▼
┌─ D0 DETECT ─────────────────────────────────────────────┐
│ dialogue(OCR) + title + watermark + top L/R up_wm + @OCR │
│ → dehardsub_regions.json (confirmed:false) + preview.jpg │
│ + outside_box_bright_blobs / top min_h 建议              │
└────────────────────────────┬────────────────────────────┘
                             ▼
┌─ D1 CONFIRM（人工或 API）────────────────────────────────┐
│ 框盖满字形+描边+下缘；顶栏 min_h；@ 跟轨或多关键           │
│ spill>0 不得盲确认；confirmed:true 后禁止再 detect 漂移  │
└────────────────────────────┬────────────────────────────┘
                             ▼
┌─ D2 PROCESS（hybrid，顺序固定）──────────────────────────┐
│ ① dialogue → glyph_black / spatial / OCR+TELEA           │
│ ② up_watermark：左顶 copyfill｜右顶 Morphic｜中景 logo洞 │
│ ③ title boxfill｜watermark edge_patch                    │
│ ④ flat 才 cleanplate（暗板拒收）                         │
│ ⑤ residual：Top-K 限修（禁止 too-hot 静默放弃）          │
│ ⑥ dialogue_safety：Δdark / w2b → 回退；终稿对账          │
│ ⑦ LaMa polish：仅 title/watermark                        │
│ ⑧ 内嵌 QA → 写 quality（ok | degraded）                  │
└────────────────────────────┬────────────────────────────┘
                             ▼
┌─ D3 QA GATE（人工加严 / CI）─────────────────────────────┐
│ 多时刻 src|baseline(v2)|clean；process-box；spill；半抹  │
└────────────────────────────┬────────────────────────────┘
                             ▼
                    clean.mp4 + dehardsub_meta.json
```

### 阶段细则（现网 vs 目标）

| 阶段 | 做什么 | 现网状态 | 目标优化（写死约定） |
|------|--------|----------|----------------------|
| **D0 Detect** | `detect_dehardsub_regions` → `resolve_hardsub_boxes` | OCR 对话 + 顶栏两侧 + `@` OCR；顶栏几何偏紧 | 顶栏 **min_h≈48–56**（勿默认 h=28）；报告 `outside_box_bright_blobs` |
| **D1 Confirm** | 审 preview、改框、`confirmed:true` | 人工可改；过矮框全片漏 | **框盖满墨**；`@` 多关键/跟轨；spill>0 不得盲确认 |
| **D2.1 Dialogue** | `classify_dialogue_route` | tiles 细亮洞 TELEA；暗景黑糊 | mask=白芯∪邻接暗描边；洞内过暗 **拒收该帧** |
| **D2.2 Up-WM** | 左顶 / Morphic / mid | 自适应有；静态漂移大 | mask 跟墨；软墨用残差检测；**半抹→整洞回退源** |
| **D2.3 Title/WM** | boxfill / edge_patch | 已落地 | 保持；禁与 dialogue plate 高 IoU 双抹 |
| **D2.4 Plate** | 仅 `flat` | ✅ + `dialogue_plate_is_safe` | tiles **永不** plate |
| **D2.5 Residual** | tiles 残影 | ✅ Top-K（64）+ meta `mode`/`hits`；默认 opt-in 因重编码会糊 plate | 保持；启用时过热勿整段 skip，关闭也写 skipped |
| **D2.6 Safety** | 整带压黑 | ✅ Δdark≥0.15 / w2b≥0.04；confirm 框；`safety_audit` | 终稿 would_restore>0 → degraded |
| **D2.7 Polish** | LaMa | ✅ mode 表 + `polish=off` 日志；仅 title/watermark | 勿默默传 floor=1.0 |
| **D2.8 / D3 QA** | `qa_pair` | ✅ flags→degraded；wm w2b/半抹；`qa_spans`/`truthful_ok` | process-box 优先 |

### Process 顺序（代码真相 · 勿改乱）

`run_multipass_cleanup` 框循环按 `regions.boxes` 顺序，再全局后处理：

1. 各框：`dialogue`（route）或 `up_watermark`（copyfill/morphic/logo）或 `title`/`watermark`  
2. `dialogue` flat → cleanplate（可拒）  
3. `dialogue` tiles → `residual_repair.scan_and_repair`  
4. `dialogue` tiles → `encode_dialogue_safety_gate`  
5. LaMa polish（`title`/`watermark` only；已 boxfill 跳过）  
6. 写 `quality`（polish + QA flags + safety_audit + byte_identical）

`caption_stems` hybrid 固定 `()`。默认 `inpaint_method=telea`（`VITUAL_DEHARDSUB_INPAINT=lama` opt-in）。  
`active_windows`：`strip_hardsubs` 算 timeline 并传入；**D8 已接通**——dialogue OCR+TELEA 窗外帧原样拷贝（水印全片仍抹）。

### 角色算法表

| role | 算法 | 硬约束 |
|------|------|--------|
| `dialogue` | `glyph_black` / `spatial_fill` / OCR+TELEA(`tiles`\|`flat`) | 禁 tiles+cleanplate；禁整带滥 TELEA；禁半清变黑字 |
| `title` | 整框 boxfill | 静态顶栏标题 |
| `watermark` | 邻域 `edge_patch` | 右下角常驻 |
| `up_watermark` | 左顶 copyfill；右顶 Morphic 自适应；中景静态/逐帧洞 | 软墨半抹→整洞回退；不做 LaMa polish |
| residual polish | LaMa | **仅** `title` / `watermark` |

### CLI / 入口

```text
# work_dir = downloads/batch/<job> 或 mode-renders/<job>
python dehardsub_workflow.py detect  <work_dir>
# 审 media/dehardsub/dehardsub_regions_preview.jpg → 改 boxes
python dehardsub_workflow.py confirm <work_dir> [ocr_telea|lama|auto]

# 单文件入口
python dehardsub_run.py --source <视频>
```

输入视频须与 detect **同坐标系**（`resolve_working_video` / `existing_source_video`）。  
media-only **禁止**提前 `strip_hardsubs`（避免双 strip + 跳过 confirm）。

Try API：`POST /api/try/dehardsub/detect` → 人工确认 → process（见 `api/app.py`）。

### 模块地图

| 职责 | 文件 |
|------|------|
| detect / strip / mode 表 | `media_ops.py` |
| hybrid 主流程 / Morphic / safety / plate | `visual_cleanup.py` |
| 对话 OCR+细洞 TELEA | `dehardsub_ocr_telea.py` |
| 对话残影扫描修补 | `residual_repair.py` |
| 路线分类 / glyph_black / spatial | `_inpaint_shared.py` |
| 多时刻 QA | `dehardsub_frame_qa.py` |
| CLI 两阶段 | `dehardsub_workflow.py` |
| 推演复现（开发用） | `_sim_dehardsub_gaps.py` / `_sim_safety_detail.py` / `_sim_detail2.py` |

---

## 实现规格 D1–D7（目标态合同）

改代码时按此验收；数字来自 BV1P6 demo v13 复算，可作回归锚点。

### 阈值：现网 → 目标

| 常量 / 行为 | 现网 | 目标 | 位置 |
|-------------|------|------|------|
| `dialogue_safety` 框 | process pad 框（易稀释） | **confirm / route 对话框**（与 QA 同框） | `encode_dialogue_safety_gate` |
| safety `max_darken` | 0.40（`dark<30`） | **0.15**，仅计数/审计；**不单独触发回退**（暗底描边字正常去字会变暗，见根因报告） | 同上 |
| safety w2b | 无 | **≥0.04 → restore 该帧** | 同上 |
| safety 终稿对账 | 无；v13 meta `restored=0` 但重放应 78–125 | process 末重放，`would_restore>0` ⇒ 不得 `quality=ok` | hybrid 收尾 |
| residual `max_hit_frac` | 0.12 → 整段 `[]` | 保留扫描；**Top-K**（如 smear 最高 24 帧）限修 | `residual_repair.py` |
| residual meta | idxs 空则不写 passes | 必写 `mode=ok\|repaired\|skipped_too_hot` + `hits` | `run_multipass_cleanup` |
| 顶栏 confirm `h` | demo 常 28 | **min_h ≈ max(32, 0.08×H)**（480p=38）；`48` 已实测把 copyfill 推到额头造成回归，勿写回合同 | detect + confirm 闸 |
| `@` / logo ink | `_logo_ink_mask_roi` V>168 | 加 **相对局部中值残差墨**；半抹（亮降+暗升）回退源 | `visual_cleanup.py` |
| TELEA 填后 | 无拒收 | 洞内均值 ≪ 环状邻域 → **ring-median 回填**（不留黑块、也不贴回源字幕） | `dehardsub_ocr_telea._render_pass` |
| QA 框 | `regions.boxes` confirm | **`passes[].box` process 实际框**优先 | `dehardsub_frame_qa.py` |
| QA 水印 | 只查 bright residual | + **w2b≥0.04**、half_wipe、spill | 同上 |
| `quality` | 只看 polish 成败 | QA flags 非空 ⇒ **`degraded`** | hybrid 收尾 |
| polish floor | demo 常传 1.0 | 跟 mode：fast≈关，balanced/quality=**0.02**；显式 1.0 打 `polish=off` | 调用方 + meta |
| top-wm method 闸 | `y0<=8 & h<=48` 与 `up_watermark_prefers_boxfill` 两套 | **统一** expand 后口径 | `encode_static_boxfill` |

### D1–D7 一览

| ID | 补丁 | 回归断言（BV1P6 demo） |
|----|------|------------------------|
| **D1** | safety↔QA/终稿对账 | 对 v13 重放 pad 框 `would_restore_040≥78`；审计与闸同口径（`min_bright=0`），成功去字的暗底描边字不得回退（回归测试） |
| **D2** | residual meta + Top-K | 复现 too-hot hits=80 时 meta 可见；Top-K 后脏帧减少 |
| **D3** | QA→quality + process-box + wm 门禁 | v13 类跑次不得 `quality=ok`；Morphic spill/w2b 有旗标 |
| **D4** | 顶栏 min_h + spill 阻断 | confirm h=28 + spill≈框内量级 → 拒绝 process |
| **D5** | `@` 残差墨 | t=0.3 左 @ 检出率 ≫ V>168 的 mean≈0.006 |
| **D6** | 洞内过暗拒收 | t=8/9 对话 w2b&lt;0.04 **或** ring-median 回填（禁止黑带，禁止贴回源字） |
| **D7** | 统一 method/expand | 单测 h=28..64 不误路由 |

### 半抹定义（工程）

同一 ROI 内同时满足则判半抹，**整洞回退源帧**：

1. `clean.bright` &lt; `src.bright × 0.2` 且 `src.bright` &gt; 0.05  
2. `clean.dark − src.dark` &gt; 0.05  
或：`white_to_black ≥ 0.04`（亮→近黑）。

原则：**留字优于灰黑残影**。

---

## 产物与 meta 契约

### 目录（相对 `work_dir`）

```
media/
  source.mp4                  # detect/process 坐标系基准
  dehardsub/
    dehardsub_regions.json    # boxes + confirmed
    dehardsub_regions_preview.jpg
    clean.mp4                 # 唯一 master
    dehardsub_meta.json       # 机器可读验收
    dehardsub_progress.json   # 运行中（可删）
    _qa_*/                    # 抽帧与 qa_report.json
```

### `dehardsub_regions.json`（目标字段）

```json
{
  "confirmed": true,
  "width": 852,
  "height": 480,
  "boxes": [
    {"x": 0, "y": 0, "w": 0, "h": 0, "role": "dialogue|title|watermark|up_watermark"}
  ],
  "gates": {
    "outside_box_bright_blobs": 0,
    "top_min_h_ok": true,
    "spill_warnings": []
  }
}
```

`gates` 已落地（D4）：不过闸默认不得 process（`--force` / API 409）。

### `dehardsub_meta.json` · `passes[]` 必填

| engine | 必填字段 | 说明 |
|--------|----------|------|
| `ocr_telea` / hybrid dialogue | `role, box, route, text_boxes_masked` | box=process 实际框 |
| `morphic_adaptive` | `morphic_copy, morphic_telea, box` | |
| `copyfill` / `logo_telea` | `mode, box, fallback` | `per_frame_ink` 时记 skip/attempt |
| `residual_repair` | `mode, hits, scanned, frames_repaired` | **禁止静默**；too-hot 也要一条 |
| `dialogue_safety` | `restored, frames, box, max_darken, w2b_thr` | 与终稿对账 |
| 顶层 | `quality, quality_note, polish_residual_floor, qa_flags` | qa_flags 目标必填 |

**读 meta 时**：`quality=ok` 若无 `qa_flags: []` 且无 residual/safety 明细 → **不可信**，必须以抽帧为准。

---

## 能力边界

**结论（2026-11-17 · BV1P6 demo+batch）：不能承诺三条「100%」。**

| 目标 | 证据（现网） | 可写 | 不可写 |
|------|--------------|------|--------|
| 通用 | 多分辨率 ROI；顶栏过紧、无时序、quality 不吃 QA | 「多分辨率、多角色 ROI 管线」 | 「任意平台零配置完美」 |
| 字幕净 | v13 有字帧失败率高；residual 80/296 too-hot | 「有字帧 QA 零旗标才交付」 | 「字幕去除效果 100%」 |
| 水印定位 | Morphic drift tens of px；h=28 裁肚；spill 未清 | 「误差≤N px 且 spill=0 才 process」 | 「水印位置精准 100%」 |

**通用性红线**：禁止把 BV1P6「收紧顶栏 h=28」写成全站默认。

**修完 D1–D7 仍做不到无痕的：** 近景皮肤软 `@`；深色褶皱上的双语 TELEA；无时序时顶栏微闪。门禁价值是 **挡住静默烂片**，不是法医级还原。

---

## 效果推演 · 查缺补漏（BV1P6 demo v13）

样片：`downloads/mode-renders/BV1P6_demo10s/`。  
复现：`_sim_dehardsub_gaps.py`、`_sim_safety_detail.py`、`_sim_detail2.py`。  
`clean_demo_v13.mp4` ≡ `clean.mp4`（同 size/mtime）。

### 确认框（过紧）

| # | role | box | 问题 |
|---|------|-----|------|
| 0 | dialogue | 188,405,480×68 | 暗景双语；TELEA 黑糊 |
| 1 | Morphic | 510,0,330×**28** | h 过矮 |
| 2 | 顶左 | 8,0,280×**28** | 同上 |
| 3 | 右 @ | 620,330,110×36 | 中景 |
| 4 | 左 @ | 110,148,100×32 | 近景贴脸 |

### 关键时刻

| t | 源 | v13 clean | 根因 |
|---|-----|-----------|------|
| 0.3 | Morphic + 双 @ 贴脸 | Morphic 残影；左 @ **仍可读**；顶左发丝横撕 | 框矮；V>168 软墨检不出；copy 贴边 |
| 4–5 | 对话 + 顶栏 | 对话压暗；Morphic 亮残 | TELEA；safety 未拦 |
| 7–9 | 双语压军装/长椅 | **黑糊带** w2b≈0.09 | 深色填白；`restored=0` |

### 现网 QA vs 目标门禁

| 事件 | 现网 qa | 目标 | process |
|------|---------|------|---------|
| 对话 darkened +0.42~0.50 | ✅ | → degraded | safety `restored=0`，quality=ok |
| 对话 w2b≈0.09 | ✅ | 同上 | safety 不看 w2b |
| Morphic wm_w2b≈0.30 | ❌ 盲区 | 旗标 | TELEA 半清 |
| 右 @ half_wipe | ❌ | 回退源 | logo 变暗斑 |
| 顶栏 spill | 无 | 阻断 | t=8 框内墨 2714 / 框下 20px **2316** |

### safety 矛盾（严重）

终稿重放（`dark<30`，Δ>+0.40）：

| 框 | 应 restore | max Δdark | max w2b |
|----|------------|-----------|---------|
| confirm | **125/300** | 0.51 | 0.096 |
| safety pad（meta 同款） | **78/300** | 0.46 | 0.110 |
| 阈 0.15 | ~150/300 | — | — |

meta：`restored=0`。t≈5s pad 把 Δdark 0.419→**0.376**（稀释不过 0.40）。另有 ~20 帧 mid-dark 灰黑字（30–90）整带闸盲区。

### residual 隐形

| 扫描 | 结果 |
|------|------|
| `max_hit_frac=0.12` | 80/296 too-hot → `[]` |
| `max_hit_frac=1.0` | 80 hits（~27%） |
| meta passes | **无 residual 条**（与「真干净」无法区分） |

### Morphic spill / 左 @ ink

| 指标 | 值 |
|------|-----|
| Morphic 框内亮均值 src→clean | 0.140 → 0.022 |
| 框下 +16px | 0.193 → **0.180**（几乎未动） |
| morphic_copy / telea | 155 / 145 |
| 左 @ ink>0.32 skip | **0**；near_empty 166；ink mean≈**0.006** |

### QA 框 ≠ process 框

| 框 | confirm（现网 QA） | process expand |
|----|-------------------|----------------|
| Morphic | 330×28 | 358×36（meta 曾 h=43） |
| 顶左 | 280×28 | 308×36 |
| 右/左 @ | 小框 | 各多出 ~1.5–2k px |

### 「修完 D1–D7」效果（诚实）

| 修复 | 效果 |
|------|------|
| D3 QA→quality | v13 **必 degraded** |
| D6 拒收 | 黑带↓；最坏留源字 |
| D2 Top-K | 补 too-hot 脏帧 |
| D4 spill | 拒 h=28 |
| D5+半抹 | 贴脸 @：检不出则留字，禁止灰残 |
| D1 对账 | 消除「meta 说 0、片已糊」 |

---

## 行业最佳实践（对齐用）

### 生产心智（不变）

1. Region → Detect → Mask → Reconstruct → QA；只动 ROI。  
2. Mask：白字+黑描边同洞；小膨胀盖抗锯齿、不吞身体。  
3. Reconstruct：优先无字关键帧+运动传播；单帧 inpaint 只填残差（现网最大差距）。  
4. 半抹禁止：半透明半清 → 黑灰残影，差于留字。  
5. 一视频一份 master；15s 一静一动 scrub，禁止单帧截图过关。

### 外参补充清单（相对本仓）

| 实践 | 来源倾向 | 本仓 |
|------|----------|------|
| **只处理有字时段**；无字帧原样拷贝 | EchoSubs / Subtitle Edit / VideoWipe | ✅ D8：dialogue 窗外 copy；水印全片仍抹 |
| **可审 Track / WipePlan**（keep|remove、时段、缩略图） | VideoWipe / VSR | ✅ D10 轻量 `wipe_plan`（无缩略图 UI） |
| **淡入淡出 mask 持有**（OCR 弱时前后多盖几帧） | VSR fade-aware | ✅ D11 ±0.25s hold；窗外 donor 仍可选 |
| **双 dilation**：flow/传播用大洞，最终 inpaint 用紧洞 | ProPainter / VSR | 单套 dilate |
| **软水印先 alpha 反合成，再只对残差 inpaint** | alpha-aware-watermark | 🔶 D5 残差墨 + D9 半抹回退；完整 alpha 反合成仍远期 |
| **静景角标 → 图像模型；动景 → 传播/视频模型** | ksmart / ProPainter 生态 | ✅ D12 运动分流 + D9 半抹回退；无光流传播 |
| **按镜头切分**再抹 | VSR PySceneDetect / UAV | 无 scene cut |
| **水印检测+跟轨**（模板/SIFT/SAM） | UAV / ghost-remover | 静态 confirm 框；`@` 漂移必漏 |
| **无损中间片 → 最终一次有损** | VSR FFV1 | ✅ D13：`_*` crf12 / ocr MJPG → 最终 18（非 FFV1） |
| **ROI 内 PSNR/SSIM/LPIPS + residual span** | VSR / ghost-remover | ✅ D14：psnr + spans；无 LPIPS |
| **Truthful stage**：像素未变不得标成功 | VSR | ✅ D14 `byte_identical` / safety_audit → degraded |
| **多边形 OCR mask**（非轴对齐大框） | VSR | 轴对齐 box + thin_mask |
| **场景阶梯诚实预期**（口播无痕 … 半透明有影） | Voxtakes | 见能力边界；对外勿统一「完美」 |

参考：[EchoSubs](https://www.echosubs.com/guides/subtitle-cleanup/remove-hardcoded-subtitles)、[Voxtakes ghosting](https://www.voxtakes.com/blog/subtitle-removal-ghosting/)、[VideoSubtitleRemover](https://github.com/SysAdminDoc/VideoSubtitleRemover)、[VideoWipe](https://github.com/KKenny0/videowipe)、[alpha-aware-watermark](https://github.com/sharkysan/alpha-aware-watermark)、[ProPainter](https://ar5iv.labs.arxiv.org/html/2309.03897v1)、UCI 扩散去硬字幕海报（OCR mask + prior 注入 + 时序注意）。

**不适合本路线：** 全屏艺术字、字幕盖脸关键部位、法医级还原、大面积半透明+强阴影、字幕占屏 >~30%、karaoke 逐词高亮。应裁切 / 接受残影 / 人工修。

---

## 外参第三轮查缺补漏（2026-11-17）

在 D1–D7（门禁/mask/框）之外，对照上表又挖出的可修项。**优先做杠杆高、不依赖重装时序引擎的。**

### D8–D14（建议编号续写）

| ID | 补丁 | 杠杆 | 说明 |
|----|------|------|------|
| **D8** | **接通 `active_windows`** | ★★★★★ | hybrid 对话 OCR+TELEA 窗外帧 copy（水印全片仍抹）。省算力 + 少抹干净段 |
| **D9** | **软水印 alpha / 残差路径** | ★★★★★ | 邻帧估 B → 反推 α → 高置信解析还原，仅低置信残差 TELEA/LaMa。专治 Morphic/软 `@`（比硬 V>168 靠谱） |
| **D10** | **有字轨计划（WipePlan 轻量版）** | ★★★★ | detect/confirm：角色 + 动作 remove\|keep（+ advisory windows）；process **按轨**跳过 keep；时段闸仍用 caption `active_windows` |
| **D11** | **fade hold + 无字关键帧采样** | ★★★★ | OCR/亮墨弱时前后各持有 N 帧；tiles 从窗外取真实背景再填残差（Voxtakes 四步的轻量版，可先不做光流） |
| **D12** | **静/动分流** | ★★★ | 顶栏静景：稳定 mask + 图像 inpaint/copy；运动或贴脸：禁瞎 TELEA，半抹回退或跟轨 |
| **D13** | **中间片无损 / 少代有损** | ★★★ | 多 pass 用 FFV1/yuv 或少次合并编码；最终 crf18 一次。减叠代糊 |
| **D14** | **QA span + ROI 指标 + truthful** | ★★★ | 残留/闪烁时间段可复跑；ROI-PSNR；`byte-identical`/`would_restore>0` 不得 `ok` |

### 更远期（有算力再上）

| 项 | 备注 |
|----|------|
| 光流传播 + 双 dilation mask | ProPainter 路线；静景勿强上 flow |
| 镜头检测切窗 | 避免跨切镜传播脏 donor |
| SAM/模板跟轨移动水印 | UAV 类；本仓 `@` 痛点 |
| 扩散/BrushNet 视频 inpaint | 研究向；非现网默认 |

### 与 D1–D7 的关系

- D1–D7 = **先别把烂片标成 ok + 修黑字/spill/软墨检测**（交付底线）。  
- D8 = **已有代码接线**，应插队（甚至可与 D1 并行）。  
- D9–D11 = 观感跃迁（软墨、时段、关键帧）仍可比「重新上 STTN」便宜。  
- D12–D14 / 远期 = 稳态与规模化。

---

## 已知失败 → 对策

| 现象 | 原因 | 状态 |
|------|------|------|
| 对话带黑条 | tiles + cleanplate | ✅ 仅 flat + plate safe |
| 对话黑字/黑糊 | TELEA 深色填；safety 不认 w2b | ✅ D1+D6（过暗 ring-median 回填；半抹才回退源字；软墨观感仍靠 D5/D9） |
| residual 该修却 skip | too-hot >12%；meta 隐形 | ✅ D2 Top-K + meta |
| Morphic/logo 残留+spill | 框矮；软墨 | ✅ D4 阻断 + D5/D9 残差/半抹回退 |
| `@` 留脸 | V>168 检不出软墨 | ✅ D5 残差墨；半抹则 D9 留源 |
| safety meta 与终稿矛盾 | 闸未约束交付 / 稀释 | ✅ D1 + `safety_audit` |
| QA 漏 wm w2b | 只查亮残 | ✅ D3 |
| 无字段也被 TELEA 抹 | `active_windows` 未接入 | ✅ D8 |
| method 双口径 | `h<=48` vs prefers_boxfill | ✅ D7 |
| 顶栏整段还原 | bright 闸过敏 | ✅ 不再整框 restore |
| 对话马赛克 | 整带 residual / 大 pad | ✅ 细洞；pad 缩小 |
| 顶栏贴脸 | 近景硬 copyfill | ✅ Morphic 自适应 |
| 误删场景字 | 全局 caption stems | ✅ `caption_stems=()` |
| 双 strip | media-only early strip | ✅ 已删 |
| `quality=ok` 但目视烂 | 不吃 QA | ✅ D3+D14（flags/spans/byte_identical/truthful） |
| polish floor 混乱 | 常传 1.0 | ✅ mode 表 + `polish=off` 日志 |
| 多 pass 越抹越糊 | 每代有损重编码 | ✅ D13 中间片 crf12（`_*`）/ ocr MJPG→最终 18 |
| 跨镜头 donor 错乱 | 无 scene cut | ⏳ 远期 |

---

## 验收协议

### 最低（每片）

```bash
# 工作目录示例
DEMO=downloads/mode-renders/BV1P6_demo10s

# 1) 读 meta：passes / safety.restored / residual / quality / polish_residual_floor
# 2) 多时刻 QA（框优先用 meta passes[].box）
python dehardsub_frame_qa.py \
  --src $DEMO/media/source.mp4 \
  --clean $DEMO/media/dehardsub/clean.mp4 \
  --regions $DEMO/media/dehardsub/dehardsub_regions.json \
  --out-dir $DEMO/media/dehardsub/_qa_gate_check
```

通过条件（目标态）：`qa_report.flags` 为空、`truthful_ok`、且 `quality=ok`。  
现网：inline QA / `byte_identical` / `safety_audit` 命中 → `quality=degraded`。

### 加严（BV1P6 类）

| 项 | 标准 |
|----|------|
| 时刻 | `src\|v2\|clean` ≥ 0.3 / 1 / 4 / 5 / 7 / 8 / 9s |
| 对话 | 无黑描边字；w2b &lt; 0.04；无整带黑糊 |
| 水印 | 不可读；允许「未动」，禁止变黑残影 |
| 顶栏 | 框盖满下缘；spill+16px 无明显亮墨 |
| scrub | 15s 一静一动，100% 缩放看条带边缘 |
| 对账 | 对 clean 重放 safety；`would_restore_040` 应≈0 |

工具：`dehardsub_ocr_telea.qc_dehardsub` / `make_compare` / `dehardsub_frame_qa.qa_pair`。

### 环境变量

| 变量 | 作用 |
|------|------|
| `VITUAL_DEHARDSUB_ENGINE` | 默认 `ocr_telea` |
| `VITUAL_DEHARDSUB_INPAINT` | `telea`（默认）/ `lama` |
| `VITUAL_DEHARDSUB_POLISH` | `off` 可关 LaMa polish |
| `VITUAL_DEHARDSUB_VLM` | detect VLM 模型名 |

---

## 落地验收表（D1–D14）

| ID | 合同要点 | 代码实态 | 结论 |
|----|----------|----------|------|
| D1 | confirm/route 框（收紧守卫回传）；Δdark≥0.15 仅审计；w2b≥0.04→回退；终稿对账 | safety 闸 + `audit_dialogue_safety_hits` 同口径（`min_bright=0`）；双极性去字成功保护抑制 edge-collapse 误回退；would_restore>0→degraded（回归测试）。**但 2026-11-21 全片实测仍有 278/1148 w2b 回退，`clear_ratio` 仅 0.585（旧 0.283），未过 0.85 目标** | **⚠️ 部分** |
| D8 | hybrid 消费 `active_windows` | dialogue OCR+TELEA 窗外 copy | **✅** |
| D3 | QA→quality；wm w2b/半抹 | inline `qa_pair`→degraded | **✅** |
| D6 | 白∪描边；洞内过暗拒收 | `thin_mask` + `_reject_dark_inpaint` → `_ring_median_fill`（不留源、不留黑块） | **✅** |
| D2 | Top-K；meta 必写 | `top_k_when_hot=64`；默认 opt-in（`VITUAL_DEHARDSUB_RESIDUAL=1`），关闭也写 `mode=skipped_v2_strength` | **✅** |
| D4 | min_h；spill **阻断** | `build_dehardsub_gates`（480p 阈值 38，`48` 已否决）；confirm/process 默认拦截（`--force` 可过） | **✅** |
| D5 | 软墨残差检测 | `_logo_ink_mask_roi` += 相对中值残差 | **✅** |
| D7 | 统一 method 闸 | `resolve_up_watermark_method`（hybrid + auto） | **✅** |
| D9 | 半抹回退源 | logo/morphic/telea 后 `_roi_half_wipe`→整帧 ROI 回源 | **✅**（轻量；完整 alpha 反合成仍可选远期） |
| D10 | WipePlan 轻量 | detect/confirm 写 `wipe_plan`；`action=keep` 跳过整轨；windows 为元数据（时段闸用 `active_windows`） | **✅** |
| D11 | fade hold | `expand_windows_fade_hold(±0.25s)` 后再进 hybrid/OCR | **✅**（窗外干净帧采样仍可选） |
| D12 | 顶栏静/动分流 | `top_strip_motion_score` → 高运动改 morphic/logo | **✅** |
| D13 | 中间片少代有损 | `_*`/中间 writer→crf12；ocr MJPG→最终 18；hybrid 末帧常 rename 交付（未必再压 18） | **✅** |
| D14 | QA span + truthful | `spans` / `psnr` / `byte_identical` → 不得 `quality=ok` | **✅** |

远期仍开：完整 alpha 反合成、WipePlan UI、窗外 donor 采样、镜头切、光流。

### 2026-11-21 分区填充策略 FILL_V2（opt-in）

第一帧逐帧精修 + 网络调研（DeepForgeHub 2026 模型对比 / 火山引擎半透明水印反算 / LaMa-fp32 掩码要点）后落地。**开关**：`VITUAL_DEHARDSUB_FILL_V2=1`（默认关，不影响既有路线）。

| 区域 | 旧法问题 | V2 法 | 依据 / 第一帧实测 |
|------|----------|-------|-------------------|
| 顶栏白字水印 | LaMa 是生成式，把水印"生成回来"；copyfill 留平涂块；**调用前** `expand_static_overlay_box` 大扩框会把 Morphic MAD 从 ~3.8 抬到 ~9（偏离 gold50） | **黄金锁**（`dehardsub_gold_workflow.wipe_top_watermarks` / `top_gold_contract`）：regions **原框** + `ns_box_wipe(feather=6,radius=5)`；字尾仅靠 `ns_box_wipe` **内部**向下探测。禁止 expand 预扩 / Morphic 贴片 / TELEA / ink 复合 / donor 贴顶栏 | 非生成不重建字；BV1P6 f0 vs `src_0001_final2` 右顶 MAD≈3.8 / 左顶≈0.9 / bright=0；见 `eval/gold/dehardsub_gold_workflow/` |
| 底部字幕（压红领章等纹理） | 紧贴笔画 mask 残影 + 二次 feather 留边缘字 | LaMa **mask 扩张 11×11**（含描边）+ 跳过二次 feather（内置羽化） | LaMa-fp32：mask 比目标大一圈；bright 0.155→0.003，红领章/勋章保留 |
| 中景左右 @（半透明） | 固定亮度阈值/静态共识 mask 抓不到（左 @ 在 V>168 下 ink≈0.006） | `adaptive_lama_watermark_wipe`：逐帧 `gray - medianBlur(15)` 残差 + gray/sat 阈值（左 >10/45/110、右 >9/50/105），膨胀 9×9/7×7 + CLOSE 5×5 后 LaMa 重建；method=`adaptive_lama` | 首帧移植验证：midL MAD 0.91 / midR 4.01（vs 精修版），亮字残留 0；左右 @ 均消失 |
| 零星残留补丁 | 整块中值/NS 在暗区留下死板色块或糊斑 | **LaMa 在扩大的上下文字块里重建**（裁 ~190×117 含衣领/领章结构，只在补丁区建 mask），再贴回 | 胸前衣领那块从平色方块→衣领斜线与军装渐变延续，无补丁感 |

**2026-11-21 精修策略正式入 worker（不再是临时脚本）**：
- `VITUAL_DEHARDSUB_WM_ADAPTIVE=1` 让中景 `up_watermark` 从 `logo` 改走 `adaptive_lama`；顶栏仍走 `ns_box`（`VITUAL_DEHARDSUB_FILL_V2=1`）。三个开关已写进 `ecosystem.config.cjs` 的 `vitual-api.env`，`pm2 start ecosystem.config.cjs --only vitual-api --update-env` 生效。
- `ns_box_wipe` 向下探测从"整段均值 >0.01"改为"下沿 12 行逐行 >0.008"，修复 regions h=48 被 cap 到 41 后光带/字尾残留（首帧 topL/topR bright 均 0）。
- NS / adaptive_lama 的 QA 返工与首遍同 seed（都从源帧重做），结果相同；已跳过以免每帧多一次 LaMa 推理。

**已知限制（不夸大）**：
- 半透明水印的**无损解法是反向 alpha 合成** `I=(J-A)/(1-α)`，但需黑白标定片求 α；本项目无标定片，多帧回归估 α 实测不稳定（噪点放大）。若日后拿到原始源，走这条可 100% 还原。
- `adaptive_lama` 依赖逐帧残差阈值，极端低对比或强运动帧仍可能漏检；全片交付前必须抽查首帧与中段帧，不能只看单帧精修图。
- VLM 适合做质量判定/脏帧检测（现有 VLM QA），不适合直接生成像素。

### 2026-11-21 工作流实测（真实素材，勿当全绿）

| 素材 | 工作流 | 对话去除率 | safety 回退 | QA / quality |
|------|--------|------------|-------------|--------------|
| `BV1P6_demo10s` | detect→confirm→process（VLM 3 轮） | 0.304（旧 v13 0.065） | 67/300 w2b | 12 flags 全在水印；degraded |
| `BV1P6N76qEuJ` 全片 | 同上（VLM_MAX=8 加速） | **0.585**（旧 0.283，fail_ratio 0.415 > 0.15 门槛） | **278/1148 w2b**；终稿 `would_restore=6` 已正确标 degraded | 16 flags（含 2 处 dialogue w2b） |

结论：route 框收紧已修（安全框从 h=27 恢复为 h=62），误回退大幅减少，但**仍有大量半抹帧被 w2b 分支回退成源**（`left_as_source` 152/443）。剩余项需先抓 pre-gate 帧钉死是 TELEA 半抹还是阈值过严，再改；不要直接放宽 gate 掩盖失败。

### 2026-11-22 safety gate mush 误判根因钉死 + 全帧校对入库

**根因（逐帧复现，非推断）**：f553 的 OCR 直出白字占比 0.1409→0.0028（擦得很干净），但最终 clean 0.1081、`ocr vs clean` dlg-mae=34——后置 `encode_dialogue_safety_gate` 把擦净的帧贴回源片。复算分支指标：`gmg=True`（0.149/0.160 源亮字已消失）、`w2b=0.143`、`mae=46`、`edge 11440→198`、`dark_share +0.08`。三个旧 mush 信号（edge collapse / dark bloom / mae）在"白字擦除成功"时**必然同时触发**；旧代码只豁免了 edge collapse，`dark_bloom` 仍触发 → RESTORE。f925/f961 同样。

**修复**：`hit_mush = (not wipe_ok) and mae>=22 and (dlt>=0.08 or edge_collapse or dark_bloom)`。`wipe_ok`（亮字或暗字已大部分消失）为真时整条 mush 分支禁用；真损坏（脸涂黑且字没擦掉）仍由 w2b/dlt 捕获。验证：f553/f925/f961 不再回退，3 个 safety 单测通过。另给 LaMa 分支补 `_reject_dark_inpaint → _ring_median_fill` 兜底（之前只 telea 有），修 f384/f410 下颌黑块。

**逐帧校对写进官方流程（强制产物）**：
- `discover/run_batch.py::_dialogue_clear_gate` 由 `step=2` 改为 **`step=1` 全帧审计**，任何 10 帧鬼影都不能靠抽样蒙混过关。
- `dialogue_clear_gate.py::audit_dialogue_clear` 报告新增 `problem_frames`：每个失败帧的帧号、时间码、标签、src/out 白字占比、mad、w2b。
- `dialogue_clear_gate.py::write_problem_contact_sheet` 生成 `media/dialogue_problem_frames.jpg`（按残留严重度挑最差的 48 帧，源/clean 并排放大，含时间码）——量化数字必须配肉眼联系表，防亮布料/皮肤误报。
- gate 日志行打印 `audited/frames`、`problem_frames` 数、联系表路径。报告仍在 `media/dialogue_clear_gate.json`。

**诊断手法**：OCR 直出 vs 最终 clean 逐帧对比白字占比 + dlg-mae，能立即区分"OCR 没检测到"与"后置 gate 贴回"。cv2 seek 不可靠，必须顺序读并严格帧号对齐。

### 2026-11-22 最终结论：dialogue safety gate 对本项目整体关闭

多轮修复（mush 全禁用 / gmg 0.55 / dlt+mae）把回退从 526→278→144→141 帧、fail_ratio 42.4%→20.8%，但仍达不到 15% 门槛。逐帧对照钉死根因链：

- f553/f925：白字擦净（OCR 直出 0.14→0.003），gate 的 mush 信号（edge/dark/mae）在"成功擦白字"时必然触发 → 贴回源。
- f1090：OCR 直出图确认字幕已擦净（白衬衫+西装，无字），最终 clean 却有完整"Yes, General." → gate 贴回（141 帧 w2b=0 走 mush）。
- **离线验证（决定性）**：把 OCR 直出当 clean 跑同一 clear gate，`fail_ratio=7.86%`（PASS）；带 gate 的 clean 为 20.8%（FAIL）。即 gate 本身是 100% 的失败来源。

**决策**：`VITUAL_DIALOGUE_SAFETY_OFF=1` 写进 ecosystem.config.cjs 的 vitual-api env，`encode_dialogue_safety_gate` 在该开关下直接把 OCR 擦除结果原样透传（copyfile），不再做任何源片回退。防"脸涂黑"由 LaMa dark-fill 兜底（`_reject_dark_inpaint→_ring_median_fill`）与 clear gate 承担；P0 防涂黑单测（test_dialogue_safety_restores_w2b）仍通过，默认路径未削弱。

**教训**：不要在没有 pre-gate 帧证据时反复按日志计数猜阈值。先用"OCR 直出 vs 最终 clean"的离线 A/B 判定 gate 是否是失败源，再决定是否整体关闭该环节。

### 2026-11-22 关键更正：真正的"完美"参照是黄金阈值 mask，不是 OCR thin_mask

用户指出全片效果远差于首帧精修版（src_0001_final2.jpg）。逐像素对齐验证（align_to_gold.py / replay_gold.py）钉死真相：

- **重放黄金脚本** `fix_frame1_final2.py` 第0帧 → MAD=0.24 完美复现，说明引擎/环境没变。
- **黄金 mask 与 OCR thin_mask 形态不同**（compare_masks.py 图证）：黄金用 `gray>90 & sat<95` + **椭圆 11×11 膨胀** + CLOSE 5×5，保留字间距缝隙；thin_mask（局部差分 + 方形 11×11）把字间缝填成实心块。LaMa 能从黄金 mask 的缝隙借周围纹理 → 军装/皮肤纹理保留；面对 thin_mask 实心块只能填平色 → 平色补丁。
- **首帧 MAD**：当前成品 vs 黄金 gold，bottom 区 10.88（差）；用黄金 mask 重做 → MAD=1.42（对齐）。

**修复**：新增 `gold_threshold_mask()`，用环境变量 `VITUAL_DEHARDSUB_GOLD_MASK=1` 开启。在 `_render_pass` 里字幕带用黄金 mask 替代 thin_mask（不叠加，避免实心），跳过亮度兜底/二次膨胀/ring_median 兜底，与 fix_frame1_final2.py 完全一致。confirm box (216,399,428,62) 内部向上扩24/向下扩8 对齐黄金带 y375-458（goldpx≈19439）。已写入 ecosystem.config.cjs。

**纠正之前的错误结论**：先前记忆说"平色补丁是物理上限、勿再调 mask"是错的——根因是 mask 形态（实心 vs 留缝），不是物理限制。黄金参照证明留缝 mask 能保留纹理。教训：用户说"某版完美"时，必须先逐像素复现那版的完整参数链（mask 生成算法也要对齐），不能只看引擎和坐标。

**最终结果（2026-11-22 22:41 全片重跑）**：job 86 `status=done`，`clear-gate PASS fail_ratio=3.1% clears=96.9% audited=1149/1149 problem_frames=23`，新 `clean.mp4` 已落盘。逐帧联系表 `%TEMP%/dehardsub_audit_20261122/final/overview_sheet.jpg` 肉眼确认字幕全部消失。

**遗留视觉问题（未修，需用户确认优先级）**：字幕虽消失，但 LaMa 在字幕区留下平色矩形补丁——独立审计 `audit_patches.py` 量化 740/1149 帧满足"源 lap≈9140 → clean lap≈11 + mae≈40"，即整条字幕带被填成平色块（联系表 f480/f680/f1120 下巴/领口处可见）。这是 thin_mask+LaMa 大面积填充的固有副作用，不影响 clear gate 通过，但影响观感。若要修，方向是 TELEA 细笔画逐像素填充或缩小 mask 只盖笔画，需另做对比验证。

## 落地优先级

0–9. **D1–D14（轻量合同）** — D1 ⚠️（见上表实测），其余按落地表 ✅  
10. 远期：光流传播 / 镜头切 / 跟轨 / 完整 alpha 反合成 / 扩散 / 窗外干净帧 donor  

---

## 模式表（postproc）

| mode | engine | polish floor |
|------|--------|--------------|
| fast | ocr_telea | 1.0（≈关） |
| balanced | ocr_telea | 0.02 |
| quality | ocr_telea | 0.02 |

调用方**勿默默传 `polish_residual_floor=1.0`** 除非刻意关并打日志。  
`dialogue_short_route` 写入 opts；hybrid 以 `classify_dialogue_route` 为准。

---

## 与「完美还原六要点」对齐

| # | 要点 | 现网 |
|---|------|------|
| 1 | 同分辨率 master（crf18） | ✅ |
| 2 | 边缘羽化 | ✅（仍可能 seam） |
| 3 | 时序平滑（孤立检测丢弃） | ✅（极淡帧可能漏） |
| 4 | 多遍默认 1 | ✅ |
| 5 | 半透明 alpha | ➖ 软 `@`/Morphic |
| 6 | 关键帧传播 | ➖ 已下线 STTN/ProPainter；最大差距 |

---

## 通用逐帧精修（opt-in，2026-11-23）

`dehardsub_frame_refine.py` 提供**通用**的逐帧残留判据 + 候选重做，不含任何视频特定帧号、坐标或固定亮度阈值；字幕框由调用方传入（OCR/region 动态给出）。

- 接入点：`dehardsub_ocr_telea._render_pass` 在写出每帧前调用 `refine_roi()`，复用当帧 OCR 框，避免二次 OCR。开关 `VITUAL_DEHARDSUB_REFINE=1`（默认关闭，风格同 `VITUAL_DEHARDSUB_FILL_V2`）。
- 判据：源帧 OCR 字形先验 G + 同帧字幕行环带 Ring 作零分布；只把**字形尺度的暗连通块**（白字被清成黑字）视为可修残留。
- 防回退：候选必须实质减少暗字形块，且不得新增亮块、亮度漂移 >10、平色/黑度/结构不连续恶化；重画面积不得超过字幕框 40%。
- 实测（BV1P6 代表帧 + 抽样 165 帧）：仅 f385/f825 类半透明字黑残留被重做；f0/f412/f610/f848/f1089/f1148 等误报帧全部保持原样。此前纯 z-score 评分器会把 f0/f1148 误改、把 f777–f819 画成亮糊斑，已否决。
- 边界：这是**保守的残留修补**，不是"自动保证逐帧完美"。阈值评分与 agent 看图选优已被实测证伪；关键帧仍需肉眼/人工复核（见项目记忆 `feedback_dehardsub_shot_refine_workflow.md`）。

测试：`pytest test_dehardsub_frame_refine.py -q`（7 项，纯合成图，不依赖具体视频）。

---

## 通用后处理（2026-11-23）：切镜重复帧 / 水印糊斑 / 亮字残影

`dehardsub_postfix.py` 在 clean.mp4 落盘后自动跑（`media_ops.strip_hardsubs` 的
`replace_file_robust` 之后；`VITUAL_DEHARDSUB_POSTFIX=0` 可 opt-out）。输入只有
source + clean + regions.json，**不含任何视频特定帧号/坐标/亮度**。

- **单遍扫描 + 帧数校对**：四类检测共用一次 source/clean 顺序解码（原来 3 遍）。
  两路各自累计精确帧数，一路结束后仍把另一路读完；输出以 **source 帧数为准**：
  clean 短则复制末帧补齐，clean 长则截断，不再静默按两流较短者输出。
  `dehardsub_postfix.json` 写 `source_frames / clean_frames / output_frames / frame_alignment`，
  `media_ops` 报告同步透传。
- **切镜重复帧**：全帧灰差 >基线×3 且 >15 的 spike（成对）即切点错位；
  用 source 帧重渲染（dialogue 框 OCR 细字形洞 + TELEA）替换。实测 BV1P6
  10 个切点共 20 帧，灰差从 29–49 降到 1.28（均值）。
- **切点帧水印供体修复**：source 帧重渲染只处理 dialogue，会把固定水印也带回来。
  切点帧的水印框从**同镜头相邻帧**借像素（框外灰差 <12 才认作同镜头；跨镜头退化
  整框 NS），1px 羽化避免硬边。实测 BV1P6 20 个切点帧 13 帧借到供体、7 帧 NS 兜底，
  水印字全部消失；比 OCR 字形 mask 借像素/LaMa 的暗影残留更干净。
- **水印白糊斑**：非 dialogue 框内，暗场景（中值<50）的亮像素（>120 且>中值+35）
  落在源暗字位置、且是小块(<框3%) → 整框 NS from source + 大半径羽化。实测 66 帧。
- **亮字水印残影**（2026-11-23 用户截图 f733 补）：源框是暗底亮字（95 分位-中值
  >15、亮像素<10%），clean 框更平（std < 源 std×0.62）但仍留低对比糊影
  （95 分位-中值≥4）→ 整框 NS + 羽化重填。该判据与「深字→亮糊斑」互补，
  修掉 `@0x.diabio` 在 f400/f700/f733 等平暗背景帧上的灰色糊斑；复杂背景帧
  （如 f900 肩部）因 std 比不满足而跳过，避免画出平色块。
- **压黑块**默认不启用（`VITUAL_DEHARDSUB_POSTFIX_DARK=1` 才开）：源亮字被擦除后
  必然变暗，与原画深色线条/胡茬在数值上不可分（实测"完美帧"/原画帧/黑字残留帧
  的暗块面积同量级），自动修会大面积破坏画面。

统计写入 `dehardsub_postfix.json`。测试：`pytest test_dehardsub_postfix.py -q`
（含 clean 短帧补齐/长帧截断回归）。BV1P6 原始 `clean_final.mp4` 与 source 均
1149 帧，逐帧配对 1149 帧，20 个切点灰差均值 1.28。

---

## 审阅与评分工作流（2026-11-23 修正）

`dehardsub_review.py` 替代临时评分脚本，落实两条最佳实践：

1. **并排审阅视频必须逐帧 hstack**：直接用 ffmpeg `hstack` 拼不同 `time_base`
   的两路流，framesync 会在切点重复/丢帧（BV1P6 f448 曾出现左源 448 / 右成片 447
   的假错帧）。`build_review_pair()` 用 OpenCV 顺序读两路逐帧拼接，再用 ffmpeg
   只 mux 成片音轨，输出帧数严格等于两路共同帧数。
2. **自动分数只是风险排序，不是画质百分制**：客观指标在平滑区域、动画、时间
   伪影上有盲点（Fora Soft 2026：flat/banding 是 VMAF/SSIM/PSNR 已知弱点；
   “human judgment wins”）。`score_risk()` 按各指标全片 p50→0 / p95→100 归一，
   输出 `risk_score`（越高=越值得人工复核）和 `human_review_priority`；不再用
   clean/source 全局 std 比惩罚“擦字成功后的平滑”，只惩罚**非字形区域**的纹理
   丢失，并把 outside/global diff 合并避免重复计分。

CLI：

```bash
.venv/Scripts/python.exe dehardsub_review.py \
  --source downloads/batch/<job>/media/source.mp4 \
  --clean downloads/batch/<job>/media/dehardsub/clean_final.mp4 \
  --regions downloads/batch/<job>/media/dehardsub/dehardsub_regions.json \
  --review-out downloads/batch/<job>/media/dehardsub/review_src_vs_final_exact.mp4 \
  --csv .../frame_risk_final.csv --json .../frame_risk_top_final.json \
  --sheet .../frame_risk_top12_final.jpg
```

官方流程：`media_ops.strip_hardsubs()` 在 postfix 后自动调用本模块，生成
`dehardsub_review.mp4`、`frame_risk.csv`、`frame_risk_top.json`、`frame_risk_top12.jpg`，
写入 `dehardsub_meta.json` 的 `report["review"]`，并在 `api/app.py` 状态回显。
`VITUAL_DEHARDSUB_REVIEW=0` 可 opt-out。测试：`pytest test_dehardsub_review.py -q`。
旧临时评分脚本的“画质分”结论作废。

---

## 文档边界

- 本文 = **目标工作流** + **实现合同 D1–D14** + **现网真相/推演** + **外参第三轮** + **验收**。  
- 代码真相以 `_inpaint_shared.py`、`visual_cleanup.py`、`dehardsub_ocr_telea.py`、`residual_repair.py`、`dehardsub_frame_qa.py`、`media_ops.py` 为准；与本文冲突时先修代码或改本文「现网」列，禁止只改文案假装已落地。  
- `DEHARDSUB_PIPELINE_PLAN.md` 中 STTN 段落过期；切片 overlap 设想仍可作 D11/远期参考。  
- 外链仅工程参考，非依赖。
