# 去马赛克三路对比与默认路由结论

> 记录：2026-11-12
> 目的：判断去马赛克默认路由（LaMa / OpenCV / CodeFormer），确认 LaMa 在真实内容上的涂抹（涂糊）表现。

## 结论（先看这个）

- **默认路由保持 LaMa**。它是唯一能真正修复「密集棋盘格马赛克」的引擎；OpenCV 在密集马赛克上基本无效，CodeFormer 在补齐权重前等同于 OpenCV。
- 当前代码默认本就为 `lama`（`visual_cleanup.py` 的 `demosaic_engine` 默认、`demosaic_codeformer.py` 顶部常量）。
- `models/lama.onnx` 已就位 → 真·LaMa 可用；`models/codeformer.onnx` **缺失** → CodeFormer 优雅降级 OpenCV（`DemosaicUnavailable` 被调用方捕获后回退），不会崩。CodeFormer 文档明示「非人脸不要用」，对人脸外马赛克本就无效，补齐权重前属冗余项。

## 对比产物（在 `instances/demosaic_test/`，已被 gitignore，不进仓库）

| 文件 | 内容 | 说明 |
|---|---|---|
| `compare_3x.mp4` | 合成样片三路对比 | 底部白字幕条 + 中部棋盘格马赛克；三栏 `OpenCV` / `CodeFormer(→OpenCV)` / `LaMa` |
| `compare_3x_real.mp4` | 真实马赛克样片三路对比 | 来自抖音真实片段，检出马赛克框 `x=48,y=528,w=96,h=48`（竖屏底部小打码） |
| `compare_lama_vs_telea.mp4` | 去字幕 LaMa vs TELEA 填充对比 | BV1erPNzeEUE source，同一 OCR 掩码仅切换填充引擎；1496×448 / 180 帧 / 3.3MB |
| `dhsub_lama.mp4` / `dhsub_telea.mp4` | 各自全分辨率单版去字幕结果 | 供单独查看（LaMa / TELEA 填充） |

`compare_3x_real.mp4` 属性：1936×388 / 30fps / 304 帧（约 10s）/ 5.7MB。

## 客观指标（合成样片，马赛克区方差，越低=修复越平滑）

| 引擎 | 方差 | 说明 |
|---|---|---|
| 原始棋盘格 | 1198.3 | — |
| OpenCV | 1182.0 | 几乎没修（密集马赛克中值模糊无效） |
| CodeFormer | 1182.0 | 无 `codeformer.onnx` → 降级 OpenCV，与左栏一致 |
| **LaMa** | **33.7** | 1198→34，真正把马赛克平滑补掉 |

## 关于「LaMa 是否涂糊 UI」

- **合成样片**马赛克下面是灰底，**测不出 UI 涂抹**。
- **真实样片**（douyin）马赛克偏小且较瞬时（生产级检测器因「不稳定/太小」会过滤掉它），是「真实但偏弱」的样本；且 96×48 紧框内 LaMa 只做框内 inpaint，**不会涂抹框外 UI**。
- 要真正判断「马赛克压在 UI/菜单/字幕上时的涂抹」，需要一段**马赛克与 UI 重叠**的真实片段——目前仓库里没找到这样的强样本。

## 样本情况（重要）

- **`bilibili_BV1erPNzeEUE` 系列无马赛克**：`source.mp4` / `BV1erPNzeEUE_dehardsub.mp4` / `BV1erPNzeEUE_keepsub_demark.mp4` 全片扫描棋盘格得分最高仅 **0.05**（≈0）。`_dehardsub` 是已清洗输出（字幕+马赛克早被去掉），不能当去马赛克样本。
- **仓库里唯一检出真实打码**的是：`downloads/batch/bilibili_E2E_20s/media/source_subs/media/publish/douyin/video.mp4`（抖音竖屏，检出 8 个马赛克候选，偏弱/瞬时）。`compare_3x_real.mp4` 即用此生成。

## 重新生成

脚本均在 `instances/demosaic_test/`（gitignore）：

```bash
# 合成样片（灰底棋盘格 + 字幕条）三路对比
.venv/Scripts/python.exe instances/demosaic_test/gen_compare_3x.py

# 真实片段三路对比（改 gen_compare_real.py 顶部 SRC 指向带马赛克的视频）
.venv/Scripts/python.exe instances/demosaic_test/gen_compare_real.py
```

底层调用：`visual_cleanup.encode_cleanup_pass(src, out, regions, do_hardsub=False, do_mosaic=True, mosaic_engine=ENGINE, mask_from=src)`；马赛克框由 `detect_mosaic_boxes`（宽松）或 `_collect_mosaic_regions`（严格、要求跨帧稳定）检测。

## LaMa vs TELEA 去字幕填充对比（隔离涂抹实验）

> 记录：2026-11-12（续）
> 背景：BV1erPNzeEUE 系列无马赛克，但底部有字幕带/UI；用户最初担心的「LaMa on UI chrome 涂糊」实际体现在**去字幕修复的填充引擎**上。此实验隔离 LaMa 与 TELEA 在字幕带/UI 上的涂抹差异。

- 方法：对 `downloads/batch/bilibili_BV1erPNzeEUE/media/source.mp4` 调 `dehardsub_ocr_telea.run()`，**同一套 OCR 掩码**（180 帧 ≈6s、109 个字幕框被遮），仅切换 `inpaint_method`：
  - 左 `lama`：神经内容识别填充（`inpaint_lama`）
  - 右 `telea`：扩散填充（`cv2.INPAINT_TELEA`）
- 产物（同目录 `instances/demosaic_test/`，gitignore）：
  - `compare_lama_vs_telea.mp4`：1496×448 / 180 帧 / 3.3MB，全帧并排（左 LaMa / 右 TELEA）
  - `dhsub_lama.mp4` / `dhsub_telea.mp4`：各自全分辨率单版去字幕结果
- 看什么：
  - **LaMa 栏**：描边字/笔画处是否出现**黑色鬼影**（`dehardsub_ocr_telea.py` 注释已标注：细字形洞在描边字上的邻居是描边本身 → LaMa 会把笔画填成黑色，实测鬼影）；字幕下 UI 是否被过度平滑成糊状。
  - **TELEA 栏**：繁忙纹理（UI/食物/背景）背后字幕是否被**羽化成糊斑晕开**。
- 状态：**人工肉眼判定完成（2026-11-12）——左 LaMa 效果优于右 TELEA**。LaMa 在字幕带/UI 上未见明显黑色鬼影或过度平滑糊斑；TELEA 在繁忙纹理背景上羽化更糊。故去字幕默认 `inpaint_method` 保持 `lama`（本就默认，**无需改代码**）。
- 复现：`instances/demosaic_test/gen_lama_vs_telea.py`（重跑两遍去字幕，较慢）；仅拼接用 `gen_lama_vs_telea_cmp.py`（读已生成单版，快）。

## 待决项（不阻塞默认路由）

1. ~~是否标记 CodeFormer 为 deprecated~~ **已完成**（commit `f02f1fb`：模块文档加 DEPRECATED 横幅 + 显式 `prefer="codeformer"` 发 `DeprecationWarning`）。是否提供 `codeformer.onnx` 权重仍待定（补齐前对人脸外马赛克无效）。
2. 如你能提供「马赛克压在 UI/字幕上」的真实片段，可重生成更有代表性的 `compare_3x_real.mp4` 再定夺涂抹风险。
3. ~~LaMa vs TELEA 去字幕填充~~ **已判定（2026-11-12）**：人工确认 LaMa（左）优于 TELEA（右），去字幕默认 `inpaint_method` 保持 `lama`，**未改代码**。
