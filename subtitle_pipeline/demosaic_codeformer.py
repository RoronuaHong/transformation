"""去马赛克后端。

引擎分工（由 visual_cleanup / media_ops 选择）：
  - demosaic_engine=sttn（默认，通用）：整段视频把马赛克框当 STTN 洞，
    适合身体/屏幕/文字/任意像素块，不限人脸。权重：models/sttn.pth
  - demosaic_engine=codeformer（仅人脸）：本模块 CodeFormer-ONNX。
    非人脸马赛克不要用——会往脸上「脑补」。缺 onnx 时退 OpenCV。
  - demosaic_engine=opencv：中值/inpaint 兜底，整块密集马赛克很弱。

本文件只实现 codeformer + OpenCV 兜底；通用路径在 sttn_inpaint.encode_sttn。

依赖（人脸路径）：
    - models/codeformer.onnx  （当前仓库默认未附带；缺失 → OpenCV）
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


def cv_fallback(
    roi: np.ndarray,
    mask: np.ndarray,
    radius: int = 7,
) -> np.ndarray:
    """OpenCV 兜底修复马赛克（无神经权重时使用）。

    整块被 mask 覆盖的密集马赛克不能走 cv2.inpaint —— 它没有未遮挡的源像素
    可插值，实际会原样返回甚至引入伪影。故整块时改用中值模糊弱化网格，
    局部马赛克才用 inpaint。
    """
    import cv2

    cov = float((mask > 0).mean())
    if cov >= 0.5:
        k = 5 if min(roi.shape[:2]) >= 8 else 3
        return cv2.medianBlur(roi, k)
    return cv2.inpaint(roi, mask, radius, cv2.INPAINT_TELEA)


def restore_mosaic_crop(
    roi: np.ndarray,
    mask: np.ndarray,
    bg: dict | None = None,
    radius: int = 7,
    *,
    use_neural: bool = True,
    prefer: str = "auto",
) -> tuple[np.ndarray, dict | None]:
    """修复一个马赛克 ROI。

    prefer:
      - auto / lama: 通用 LaMa（任意马赛克）→ 失败再 OpenCV
      - codeformer: 仅人脸 CodeFormer → 失败再 OpenCV
      - opencv: 直接 OpenCV
    use_neural=False 强制 OpenCV（兼容旧调用）。
    """
    if not use_neural or prefer == "opencv":
        return cv_fallback(roi, mask, radius), bg

    want = (prefer or "auto").strip().lower()
    if want in ("auto", "lama"):
        try:
            from inpaint_lama import lama_inpaint

            # LaMa 吃整帧语义；这里 ROI 当小图喂入也能修网格块。
            restored = lama_inpaint(roi, mask)
            return restored, bg
        except Exception:
            if want == "lama":
                return cv_fallback(roi, mask, radius), bg

    if want in ("auto", "codeformer"):
        h, w = roi.shape[:2]
        aspect = (w / max(1, h)) if h else 99.0
        if aspect <= 2.2 and aspect >= 0.45 and min(h, w) >= 24:
            try:
                restored = _restore_face(roi)
                cov = float((mask > 0).mean())
                if cov >= 0.5:
                    return restored, bg
                mask3 = (mask.astype(np.float32) / 255.0)[..., None]
                out = (
                    roi.astype(np.float32) * (1.0 - mask3)
                    + restored.astype(np.float32) * mask3
                )
                return out.clip(0, 255).astype(np.uint8), bg
            except DemosaicUnavailable:
                pass
            except Exception:  # noqa: BLE001
                pass

    return cv_fallback(roi, mask, radius), bg
