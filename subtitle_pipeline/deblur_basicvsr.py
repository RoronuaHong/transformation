"""去模糊：基于 ONNX 的逐帧超分去模糊（Real-ESRGAN x4 等），用已装的 onnxruntime 推理。

为什么不用 BasicVSR++ (mmedit/mmcv)：本环境是 Python3.12 + torch2.6 + cu124，
mmcv 无预编译 wheel 且源码编译失败（已实测），故改用 ONNX 路线，绕开 mmcv。
onnxruntime 已在 venv 中（1.28.0），CUDA EP 可用时走 GPU。

接入方式（由 media_ops.run_postproc 的 "deblur" 阶段调用）：
    from deblur_basicvsr import deblur_video, DeblurUnavailable
    deblur_video(src, dest, engine="realesrgan", max_height=720)

依赖（放 subtitle_pipeline/models/，缺失时抛 DeblurUnavailable，调用方跳过该阶段）：
    - models/realesrgan_x4.onnx  （Real-ESRGAN x4 超分去模糊权重，社区 ONNX 导出）
    - 可选 models/0001.onnx 等同名权重由 engine 决定；默认引擎 realesrgan
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

MODELS_DIR = Path(__file__).resolve().parent / "models"

# engine -> onnx 文件名
DEBLUR_ONNX = {
    "realesrgan": MODELS_DIR / "realesrgan_x4.onnx",
    "basicvsr++": MODELS_DIR / "realesrgan_x4.onnx",  # 降级：BasicVSR++ 不可用，复用超分去模糊
}
# Torch .pth 备用（仓库常有 realesrgan_x4.pth 但无 onnx）
DEBLUR_PTH = MODELS_DIR / "realesrgan_x4.pth"
DEFAULT_DEBLUR_ENGINE = "realesrgan"

_SESSION = {}  # engine -> onnxruntime.InferenceSession
_TORCH_MODEL = {}  # engine -> (device, model)


class DeblurUnavailable(RuntimeError):
    """去模糊模型不可用（缺 onnx 权重/推理失败）——调用方应跳过 deblur 阶段。"""


def deblur_weights_available(engine: str = DEFAULT_DEBLUR_ENGINE) -> Path | None:
    """Return onnx path if present; else None (ffmpeg fallback still usable)."""
    weight = DEBLUR_ONNX.get(engine, DEBLUR_ONNX[DEFAULT_DEBLUR_ENGINE])
    if weight.is_file() and weight.stat().st_size > 1_000_000:
        return weight
    return None


def deblur_video_ffmpeg(
    src: Path | str,
    dest: Path | str,
    *,
    strength: str = "medium",
) -> Path:
    """CPU fallback when RealESRGAN ONNX is missing: unsharp + mild denoise.

    Not as strong as RealESRGAN, but the deblur WF must not be a silent no-op.
    """
    from fetch_media import find_ffmpeg

    src = Path(src)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # light / medium / strong → unsharp luma amount
    amt = {"light": 0.6, "medium": 1.0, "strong": 1.4}.get(
        (strength or "medium").strip().lower(), 1.0
    )
    # cas (contrast adaptive sharpen) if available; else unsharp.
    vf = f"unsharp=5:5:{amt}:5:5:0.0,hqdn3d=1.2:1.2:3:3"
    ffmpeg = find_ffmpeg()
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "copy",
        str(dest),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0 or not dest.is_file() or dest.stat().st_size < 800:
        # Retry without audio copy (some snips are silent).
        cmd2 = [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(src),
            "-vf",
            vf,
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            str(dest),
        ]
        r2 = subprocess.run(cmd2, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r2.returncode != 0 or not dest.is_file():
            err = (r.stderr or r2.stderr or "")[:400]
            raise RuntimeError(f"ffmpeg deblur failed: {err}")
    return dest


def _ort_providers() -> list:
    try:
        import onnxruntime as ort

        avail = ort.get_available_providers()
        if "CUDAExecutionProvider" in avail:
            return ["CUDAExecutionProvider", "CPUExecutionProvider"]
        return ["CPUExecutionProvider"]
    except Exception:
        return ["CPUExecutionProvider"]


def _load_session(engine: str):
    if engine in _SESSION:
        return _SESSION[engine]
    weight = DEBLUR_ONNX.get(engine, DEBLUR_ONNX[DEFAULT_DEBLUR_ENGINE])
    if not weight.is_file():
        raise DeblurUnavailable(f"missing deblur onnx: {weight}")
    try:
        import onnxruntime as ort

        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        sess = ort.InferenceSession(str(weight), sess_options=so, providers=_ort_providers())
        _SESSION[engine] = sess
        return sess
    except Exception as exc:  # noqa: BLE001
        raise DeblurUnavailable(f"onnx load failed: {exc}") from exc


def _load_torch_model(engine: str):
    key = f"{engine}:pth"
    if key in _TORCH_MODEL:
        return _TORCH_MODEL[key]
    if not DEBLUR_PTH.is_file():
        raise DeblurUnavailable(f"missing deblur pth: {DEBLUR_PTH}")

    import torch
    from torch import nn
    from torch.nn import functional as F

    class SRVGGNetCompact(nn.Module):
        def __init__(
            self,
            num_in_ch: int = 3,
            num_out_ch: int = 3,
            num_feat: int = 64,
            num_conv: int = 32,
            upscale: int = 4,
        ) -> None:
            super().__init__()
            self.upscale = upscale
            self.body = nn.ModuleList()
            self.body.append(nn.Conv2d(num_in_ch, num_feat, 3, 1, 1))
            self.body.append(nn.PReLU(num_parameters=num_feat))
            for _ in range(num_conv):
                self.body.append(nn.Conv2d(num_feat, num_feat, 3, 1, 1))
                self.body.append(nn.PReLU(num_parameters=num_feat))
            self.body.append(nn.Conv2d(num_feat, num_out_ch * upscale * upscale, 3, 1, 1))
            self.upsampler = nn.PixelShuffle(upscale)

        def forward(self, x):
            out = x
            for layer in self.body:
                out = layer(out)
            out = self.upsampler(out)
            base = F.interpolate(x, scale_factor=self.upscale, mode="nearest")
            return out + base

    raw = torch.load(DEBLUR_PTH, map_location="cpu")
    state = raw.get("params_ema") or raw.get("params") or raw
    model = SRVGGNetCompact()
    model.load_state_dict(state, strict=True)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = model.to(device).eval()
    _TORCH_MODEL[key] = (device, model)
    return device, model


def _infer_frame(
    sess,
    frame_bgr: np.ndarray,
    out_size: tuple[int, int] | None = None,
) -> np.ndarray:
    """对单帧跑一次超分去模糊，返回 BGR uint8。

    out_size=(w,h) 指定输出分辨率（默认与输入同尺寸）。超分模型（x4）的输出
    会直接重采样到 out_size，避免「先缩回降采样尺寸、再放大」的二次损失。
    """
    import cv2

    # Real-ESRGAN x4 onnx 约定：输入 [1,3,H,W] float32 (0..1)，输出 [1,3,4H,4W]。
    h, w = frame_bgr.shape[:2]
    inp = (frame_bgr.astype(np.float32) / 255.0)[..., ::-1].transpose(2, 0, 1)  # BGR->RGB, CHW
    inp = np.expand_dims(inp, axis=0)
    out = sess.run(None, {sess.get_inputs()[0].name: inp})[0][0]  # [3,4H,4W]
    out = np.clip(out, 0, 1)
    out = (out.transpose(1, 2, 0)[..., ::-1] * 255.0).astype(np.uint8)  # RGB->BGR
    ow, oh = out_size if out_size else (w, h)
    if out.shape[1] != ow or out.shape[0] != oh:
        out = cv2.resize(out, (ow, oh), interpolation=cv2.INTER_LANCZOS4)
    return out


def _infer_frame_torch(
    device: str,
    model,
    frame_bgr: np.ndarray,
    out_size: tuple[int, int] | None = None,
) -> np.ndarray:
    import cv2
    import torch

    h, w = frame_bgr.shape[:2]
    inp = (frame_bgr.astype(np.float32) / 255.0)[..., ::-1].transpose(2, 0, 1)
    ten = torch.from_numpy(np.ascontiguousarray(inp)).unsqueeze(0).to(device)
    with torch.inference_mode():
        out = model(ten).clamp_(0.0, 1.0)[0].detach().cpu().numpy()
    out = (out.transpose(1, 2, 0)[..., ::-1] * 255.0).astype(np.uint8)
    ow, oh = out_size if out_size else (w, h)
    if out.shape[1] != ow or out.shape[0] != oh:
        out = cv2.resize(out, (ow, oh), interpolation=cv2.INTER_LANCZOS4)
    return out


def deblur_video(
    src: Path | str,
    dest: Path | str,
    *,
    engine: str = DEFAULT_DEBLUR_ENGINE,
    max_height: int = 720,
    allow_ffmpeg_fallback: bool = True,
) -> Path:
    """对 src 视频做逐帧去模糊，输出 dest（保留原音频）。

    max_height: 超过则先降采样再推理，控制 6GB 显存占用。
    返回 dest。缺 ONNX 时默认走 ffmpeg unsharp 兜底（不再静默跳过）。
    """
    src = Path(src)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    infer_backend = "onnx"
    try:
        sess = _load_session(engine)
    except DeblurUnavailable:
        try:
            sess = _load_torch_model(engine)
            infer_backend = "torch"
            print(f"[deblur] ONNX missing → torch PTH fallback ({DEBLUR_PTH.name})", flush=True)
        except DeblurUnavailable:
            if allow_ffmpeg_fallback:
                print(
                    f"[deblur] ONNX missing → ffmpeg unsharp fallback "
                    f"(want {DEBLUR_ONNX.get(engine, DEBLUR_ONNX[DEFAULT_DEBLUR_ENGINE]).name})",
                    flush=True,
                )
                return deblur_video_ffmpeg(src, dest, strength="medium")
            raise

    import cv2

    cap = cv2.VideoCapture(str(src))
    if not cap.isOpened():
        raise RuntimeError(f"cannot open video: {src}")
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 25.0) or 25.0
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    if w < 2 or h < 2:
        cap.release()
        raise RuntimeError(f"bad video size: {src}")

    # 降采样仅用于推理提速/控显存；输出必须恢复原始分辨率，否则会静默降低画质。
    orig_w, orig_h = w, h
    scale = 1.0
    if max_height and h > max_height:
        scale = max_height / h
    iw, ih = max(2, int(round(w * scale))), max(2, int(round(h * scale)))
    if ih % 2:
        ih += 1
    if iw % 2:
        iw += 1

    tmp_vid = dest.with_name(dest.stem + "_deblur_noa.mp4")
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    # 写回原始分辨率
    vw = cv2.VideoWriter(str(tmp_vid), fourcc, fps, (orig_w, orig_h))
    prev: np.ndarray | None = None
    prev_mix = 0.0 if infer_backend == "torch" else 0.25
    while True:
        ok, f = cap.read()
        if not ok:
            break
        if scale != 1.0:
            f = cv2.resize(f, (iw, ih), interpolation=cv2.INTER_AREA)
        # 一步到位：超分输出直接重采样到原始分辨率
        if infer_backend == "torch":
            device, model = sess
            out = _infer_frame_torch(device, model, f, out_size=(orig_w, orig_h))
        else:
            out = _infer_frame(sess, f, out_size=(orig_w, orig_h))
        # 轻量时序融合：与上一帧做 0.25 权重中值，抑制逐帧抖动。
        if prev_mix > 0.0 and prev is not None and prev.shape == out.shape:
            out = cv2.addWeighted(out, 1.0 - prev_mix, prev, prev_mix, 0)
        prev = out
        vw.write(out)
    cap.release()
    vw.release()

    from fetch_media import find_ffmpeg

    ffmpeg = find_ffmpeg()
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(tmp_vid)]
    has_audio = True
    try:
        from media_ops import _media

        has_audio = _media().has_audio_stream(src)
    except Exception:
        has_audio = True
    if has_audio:
        cmd += ["-i", str(src), "-map", "0:v:0", "-map", "1:a:0?", "-c:a", "aac", "-shortest"]
    else:
        cmd += ["-an"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", str(dest)]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    try:
        tmp_vid.unlink(missing_ok=True)
    except OSError:
        pass
    if r.returncode != 0 or not dest.is_file():
        err = (r.stderr or "")[:400]
        raise RuntimeError(f"deblur mux failed ({r.returncode}): {err}")
    return dest
