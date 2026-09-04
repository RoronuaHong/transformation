"""LaMa 单帧修复引擎（ONNX）：静态字幕最优、无时序伪影、CPU 可跑。

对齐 VSR（video-subtitle-remover）双修复器架构：
  - STTN：时序补全，动态背景强，但大洞有生成伪影
  - LaMa：单帧大 mask 修复 SOTA（Fourier Convolutions），静态字幕/复杂纹理最优

依赖（放 subtitle_pipeline/models/，缺失时抛 LamaUnavailable，调用方降级 STTN）：
  - models/lama.onnx   （big-lama ONNX 导出，固定 512×512 输入）
    来源：https://huggingface.co/Carve/LaMa-ONNX （文件 lama_fp32.onnx，约 196MB）
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

MODELS_DIR = Path(__file__).resolve().parent / "models"
LAMA_WEIGHTS = MODELS_DIR / "lama.onnx"
LAMA_SIZE = 512  # Carve/LaMa-ONNX 固定输入

_SESSION = None


class LamaUnavailable(RuntimeError):
    """LaMa 不可用（缺 onnx 权重/推理失败）——调用方应降级 STTN/其他引擎。"""


def _ort_providers() -> list:
    try:
        import onnxruntime as ort

        avail = ort.get_available_providers()
        if "CUDAExecutionProvider" in avail:
            return ["CUDAExecutionProvider", "CPUExecutionProvider"]
        return ["CPUExecutionProvider"]
    except Exception:
        return ["CPUExecutionProvider"]


def _load_session():
    global _SESSION
    if _SESSION is not None:
        return _SESSION
    if not LAMA_WEIGHTS.is_file():
        raise LamaUnavailable(f"missing lama onnx: {LAMA_WEIGHTS}")
    try:
        import onnxruntime as ort

        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        _SESSION = ort.InferenceSession(
            str(LAMA_WEIGHTS), sess_options=so, providers=_ort_providers()
        )
        return _SESSION
    except Exception as exc:  # noqa: BLE001
        raise LamaUnavailable(f"lama onnx load failed: {exc}") from exc


def _run_sess(sess, img: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """按 session 实际输入名喂参；兼容单输入(拼接)与双输入模型。"""
    names = [i.name for i in sess.get_inputs()]
    if len(names) >= 2:
        feeds = {names[0]: img, names[1]: mask}
    else:
        # 单输入模型：img 与 mask 在通道维拼接（LaMa 常见导出变体）
        feeds = {names[0]: np.concatenate([img, mask], axis=1)}
    return sess.run(None, feeds)[0]


def lama_inpaint(frame_bgr: np.ndarray, mask_u8: np.ndarray) -> np.ndarray:
    """对单帧做 LaMa 修复。mask_u8: 255=待修复区域。返回与输入同尺寸 BGR uint8。

    推理在 512×512 上完成；输出上采样回原尺寸后**只在 mask 区域采用**，
    背景 1:1 保留原生细节（不受上采样损失影响）。
    不可用/失败抛 LamaUnavailable，由调用方决定降级。
    """
    import cv2

    sess = _load_session()
    H, W = frame_bgr.shape[:2]
    img = cv2.resize(frame_bgr, (LAMA_SIZE, LAMA_SIZE), interpolation=cv2.INTER_AREA)
    m = cv2.resize(mask_u8, (LAMA_SIZE, LAMA_SIZE), interpolation=cv2.INTER_NEAREST)
    img_blob = (
        (img.astype(np.float32) / 255.0)[..., ::-1].transpose(2, 0, 1)[None]
    )  # BGR->RGB, [1,3,512,512]
    mask_blob = (m.astype(np.float32) / 255.0)[None, None]  # [1,1,512,512]
    out = _run_sess(sess, img_blob, mask_blob)[0]  # [3,512,512]
    out = np.clip(out, 0.0, 1.0)
    out = (out.transpose(1, 2, 0)[..., ::-1] * 255.0).astype(np.uint8)  # RGB->BGR
    if out.shape[:2] != (H, W):
        out = cv2.resize(out, (W, H), interpolation=cv2.INTER_LANCZOS4)
    # 只在 mask 区域采用（软边缘羽化，避免接缝）
    soft = cv2.GaussianBlur(mask_u8, (5, 5), 0).astype(np.float32) / 255.0
    a = soft[..., None]
    res = frame_bgr.astype(np.float32) * (1.0 - a) + out.astype(np.float32) * a
    return np.clip(res, 0, 255).astype(np.uint8)
