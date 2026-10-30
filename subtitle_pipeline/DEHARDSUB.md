# 去硬字幕路线（dehardsub）

B站硬字幕的**首选默认路线**：`dehardsub_ocr_telea.py`（OCR 细字形洞 + cv2.TELEA 逐帧填充）。
下文记录设计取舍、与"完美还原六要点"的对齐状态、已知限制、以及引擎接入方式。

## 路线选型（2026-10-22 用户明令"以后首选"）
- **STTN tiles**（`glyph_mask_hybrid` + STTN 时序）在持久/白字字幕上会填出大块平灰补丁（BV1aHug63EXf 右下水印、BV1oFJ16BEMT 测试均复现），质感不合格。
- **OCR 细字形洞 + TELEA**：逐帧 OCR 取字幕框 → 在框内取贴合笔画的细字形洞（轻膨胀盖描边）→ TELEA 局部插值。洞细、四周全是真实画面纹理，填充从周边延续，**不会平灰**。

## 与"完美还原六要点"对齐状态
1. **无损导出**：成品与源同分辨率；带水印的源因无干净段可流拷贝，只用 crf18（视觉无损区间）。✅
2. **边缘羽化**：文字本体权重恒 1（保证去除彻底不留鬼影），只在外扩环带内按高斯权重与原始背景融合。✅
3. **时序平滑**：两阶段（先逐帧 OCR 收集目标框 → 再渲染）；邻帧无 IoU≥0.3 支持则丢弃孤立单帧检测；**刻意不向邻帧扩散**（否则在无字幕帧白白抹背景纹理）。✅
4. **多遍迭代**：`passes≥2` 实测是**净损失**（迭代遍多遮大量框、遮罩外扩波及格外场景文字、误删 0→2），默认关闭。✅（默认关）
5. **半透明 alpha**：本路线针对不透明字幕，无需 alpha 通道处理。➖ 不需
6. **光流 / ProPainter 时序建模**：解决"手臂/衣领涂抹"的下一档。`engine="propainter"` 已接入（`propainter_inpaint.py`），但需要 **CUDA GPU + 模型权重**（首次自动下载），当前环境未跑通。➖ 权重/算力缺失

## 流程（流式，长片不爆内存）
- `_iter_frames` 惰性逐帧读取，逐帧 OCR 只存框不存帧。
- `run`：OCR 收集 → 时序平滑 → TELEA 填充（带内羽化）→ 写中间 AVI → ffmpeg 封 mp4。
- `qc_dehardsub`：源/成品真实 diff 验收 —— 目标框几乎没变 = 漏删；非目标文字框变了 = 误删。
- `make_compare`：左右对比视频。

## 指标（BV1oFJ16BEMT，3253 帧）
- 漏删 **5/3294（0.15%，全是 1–2 帧极淡字，被时序平滑过滤）** / 误删 **0**。
- 引擎默认 `passes=1`（多遍迭代默认关，见要点 4）。

## 引擎接入
`run_multipass_cleanup(engine="ocr_telea")` 直接走本路线（自带入 OCR，无需外部 box；
`watermark=None` 时自动探测角部常驻 logo）。注册于 `media_ops.DEHARDSUB_ENGINES`。

一条命令端到端（去字→QC→对比）：`python dehardsub_run.py --source <视频>`。B站 路径自动选 `ocr_telea`；`--band x,y,w,h` 可绕过 VLM 直接指定字幕带；`--engine propainter` 走 GPU 时序修复（见要点 6）。
- **全局默认仍是 `sttn`**：`ocr_telea` 经管线运行时使用 locate 出的 box，对 B站 内容安全；
  但盲目翻全局默认会影响非 B站 内容（忽略 box、写死 BV 字幕带坐标会漏删带外浮动字幕）。
  因此 `media_ops._default_dehardsub_engine_for` **仅对路径含 bilibili/BV 的 B站 任务默认 ocr_telea**，
  其余保持 sttn。
- 可选 `engine="propainter"` 走时序修复（见要点 6）。

## 已知限制 / 待办
- 涂抹痕：文字压在皮肤/衣领强纹理上时 TELEA 局部插值略糊，动态观看不明显；彻底解决需 ProPainter。
- 残留：1–2 帧极淡字被时序平滑过滤，任何"第二遍"都抓不到（已实测验证）。
- 多遍迭代、带内抛光第二遍均实测为净损失，已回退并记录于项目记忆。
