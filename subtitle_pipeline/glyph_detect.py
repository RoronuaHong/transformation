"""OCR 字形定位（VSR 同款路线）：RapidOCR 检测字幕行 bbox → 精确字形 mask。

为什么用 OCR 而不是启发式对比度：业界标准（video-subtitle-remover）都是
「OCR 定位文本行 → 只修复文本区域」。OCR 对白底黑字/深底白字/彩色字/描边
全部通用，不依赖任何背景/前景假设。

依赖：rapidocr-onnxruntime（纯 ONNX，包内自带中英文模型，离线可用）。
未安装或推理失败 → GlyphDetectUnavailable，调用方回退启发式字形检测。
"""
from __future__ import annotations

import numpy as np

_OCR = None  # RapidOCR 实例（进程级缓存）
_OCR_FAILED = False


class GlyphDetectUnavailable(RuntimeError):
    """OCR 不可用（未安装/加载失败）——调用方应回退启发式检测。"""


def _get_ocr():
    global _OCR, _OCR_FAILED
    if _OCR is not None:
        return _OCR
    if _OCR_FAILED:
        raise GlyphDetectUnavailable("rapidocr previously failed to load")
    try:
        from rapidocr_onnxruntime import RapidOCR

        _OCR = RapidOCR()
        return _OCR
    except Exception as exc:  # noqa: BLE001
        _OCR_FAILED = True
        raise GlyphDetectUnavailable(f"rapidocr load failed: {exc}") from exc


def _extract_pts(item) -> np.ndarray | None:
    """从 rapidocr 结果项提取 4×2 box 点集；结构不符返回 None。

    use_rec=False → item 即 box（item[0] 是单个点 ndim=1）
    use_rec=True  → item=[box(4×2), text, score]
    """
    if not isinstance(item, (list, tuple)) or not item:
        return None
    try:
        a0 = np.asarray(item[0], dtype=np.float32)
    except (TypeError, ValueError):
        return None
    if a0.ndim == 2 and a0.shape[1] == 2:
        return a0  # [box, text, score] 结构
    try:
        a = np.asarray(item, dtype=np.float32)
    except (TypeError, ValueError):
        return None
    if a.ndim == 2 and a.shape[1] == 2 and a.shape[0] >= 3:
        return a  # item 即点集
    return None


def detect_text_boxes(
    frame_bgr: np.ndarray,
    *,
    score_thr: float = 0.5,
    limit_box: dict[str, int] | None = None,
) -> list[dict[str, int]]:
    """OCR 检测文本行，返回 bbox 列表（全帧坐标）。

    limit_box: 只保留与该框相交的行（限制在字幕框内，避免弹幕/水印误检）。
    """
    ocr = _get_ocr()
    result, _elapse = ocr(frame_bgr, use_det=True, use_cls=False, use_rec=False)
    boxes: list[dict[str, int]] = []
    H, W = frame_bgr.shape[:2]
    for item in result or []:
        # rapidocr 返回结构随参数变化：
        #   use_rec=False → item 即 box（4×2 点集，item[0] 是单个点 ndim=1）
        #   use_rec=True  → item=[box(4×2), text, score]
        pts = _extract_pts(item)
        if pts is None:
            continue
        # use_rec=False 无分数；有分数时按阈值过滤
        try:
            if isinstance(item, (list, tuple)) and len(item) >= 3 and item[2] is not None:
                if float(item[2]) < score_thr:
                    continue
        except (TypeError, ValueError):
            pass
        x0, y0 = pts.min(axis=0)
        x1, y1 = pts.max(axis=0)
        bx = {
            "x": int(max(0, x0)),
            "y": int(max(0, y0)),
            "w": int(min(W, x1 + 1) - max(0, x0)),
            "h": int(min(H, y1 + 1) - max(0, y0)),
        }
        if bx["w"] < 4 or bx["h"] < 4:
            continue
        if limit_box is not None:
            ix0 = max(bx["x"], limit_box["x"])
            iy0 = max(bx["y"], limit_box["y"])
            ix1 = min(bx["x"] + bx["w"], limit_box["x"] + limit_box["w"])
            iy1 = min(bx["y"] + bx["h"], limit_box["y"] + limit_box["h"])
            if ix1 <= ix0 or iy1 <= iy0:
                continue
            # 裁剪到交集
            bx = {"x": ix0, "y": iy0, "w": ix1 - ix0, "h": iy1 - iy0}
        boxes.append(bx)
    return boxes


def detect_text_mask(
    frame_bgr: np.ndarray,
    *,
    score_thr: float = 0.5,
    limit_box: dict[str, int] | None = None,
    pad: int = 3,
) -> np.ndarray:
    """OCR 行 bbox → 全帧 uint8 mask（255=字形区域）。

    bbox 外扩 pad 像素盖住描边；不可用时返回全零（调用方回退启发式）。
    """
    H, W = frame_bgr.shape[:2]
    mask = np.zeros((H, W), dtype=np.uint8)
    try:
        boxes = detect_text_boxes(frame_bgr, score_thr=score_thr, limit_box=limit_box)
    except GlyphDetectUnavailable:
        return mask
    except Exception:  # noqa: BLE001 — 单帧 OCR 失败不该中断流水线
        return mask
    for b in boxes:
        x0 = max(0, b["x"] - pad)
        y0 = max(0, b["y"] - pad)
        x1 = min(W, b["x"] + b["w"] + pad)
        y1 = min(H, b["y"] + b["h"] + pad)
        mask[y0:y1, x0:x1] = 255
    return mask
