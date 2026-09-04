"""去马赛克：人脸区域用 CodeFormer-ONNX 神经修复，文字/通用区域退回 OpenCV inpaint。

为什么不用 basicsr/facexlib：本环境 Python3.12 + torch2.6 + cu124 装不上 mmcv
（basicsr/facexlib 都依赖 mmcv，源码编译实测失败），故改用 ONNX 路线，
用已装的 onnxruntime 推理，绕开 mmcv。

接入方式（由 visual_cleanup.encode_cleanup_pass 调用）：
    from demosaic_codeformer import restore_mosaic_crop, DemosaicUnavailable
    restored, bg = restore_mosaic_crop(roi, mask, bg, radius=7)

依赖（放 subtitle_pipeline/models/，缺失时抛 DemosaicUnavailable，调用方退回 inpaint）：
    - models/codeformer.onnx  （CodeFormer 人脸修复权重，社区 ONNX 导出，输入 512x512）
    - 可选 models/codeformer_fp32.onnx 等同名；默认文件名 codeformer.onnx

注意：CodeFormer 只擅长「人脸」马赛克；文字条/通用像素块它无能为力，
调用方仍会对那些区域走 OpenCV inpaint（见 visual_cleanup 中的降级分支）。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

MODELS_DIR = Path(__file__).resolve().parent / "models"
CODEFORMER_WEIGHTS = MODELS_DIR / "codeformer.onnx"

_SESSION = None  # onnxruntime.InferenceSession


class DemosaicUnavailable(RuntimeError):
    """CodeFormer 不可用（缺 onnx 权重/推理失败）——调用方应退回 OpenCV inpaint。"""


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
    if not CODEFORMER_WEIGHTS.is_file():
        raise DemosaicUnavailable(f"missing CodeFormer onnx: {CODEFORMER_WEIGHTS}")
    try:
        import onnxruntime as ort

        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        _SESSION = ort.InferenceSession(
            str(CODEFORMER_WEIGHTS), sess_options=so, providers=_ort_providers()
        )
        return _SESSION
    except Exception as exc:  # noqa: BLE001
        raise DemosaicUnavailable(f"onnx load failed: {exc}") from exc


def _restore_face(roi_bgr: np.ndarray) -> np.ndarray:
    """对整张 ROI 跑一次 CodeFormer，返回与原图同尺寸 BGR uint8。

    多数 CodeFormer-ONNX 接受 512x512 输入、输出 512x512。这里把 ROI resize 到 512 推理，
    再 resize 回原尺寸，对马赛克裁剪块足够（无需人脸对齐/检测，省去 facexlib）。
    """
    import cv2

    sess = _load_session()
    size = 512
    inp = cv2.resize(roi_bgr, (size, size), interpolation=cv2.INTER_AREA)
    blob = (inp.astype(np.float32) / 255.0)[..., ::-1].transpose(2, 0, 1)  # BGR->RGB CHW
    blob = np.expand_dims(blob, axis=0)
    out = sess.run(None, {sess.get_inputs()[0].name: blob})[0][0]  # [3,512,512]
    out = np.clip(out, 0, 1)
    out = (out.transpose(1, 2, 0)[..., ::-1] * 255.0).astype(np.uint8)  # RGB->BGR
    if out.shape[:2] != roi_bgr.shape[:2]:
        out = cv2.resize(out, (roi_bgr.shape[1], roi_bgr.shape[0]), interpolation=cv2.INTER_AREA)
    return out


def restore_mosaic_crop(
    roi: np.ndarray,
    mask: np.ndarray,
    bg: dict | None = None,
    radius: int = 7,
) -> tuple[np.ndarray, dict | None]:
    """修复一个马赛克 ROI。

    roi  : BGR uint8（裁剪出的马赛克区域）
    mask : 单通道 uint8，马赛克像素=255
    返回 (修复后 ROI, bg) —— bg 透传（本模型无时序背景，原样返回）。
    不抛异常：内部失败直接回退 OpenCV inpaint，保证流水线不中断。
    """
    try:
        restored = _restore_face(roi)
        mask3 = (mask.astype(np.float32) / 255.0)[..., None]
        out = roi.astype(np.float32) * (1.0 - mask3) + restored.astype(np.float32) * mask3
        return out.clip(0, 255).astype(np.uint8), bg
    except DemosaicUnavailable:
        import cv2

        return cv2.inpaint(roi, mask, radius, cv2.INPAINT_TELEA), bg
    except Exception:  # noqa: BLE001
        import cv2

        return cv2.inpaint(roi, mask, radius, cv2.INPAINT_TELEA), bg
