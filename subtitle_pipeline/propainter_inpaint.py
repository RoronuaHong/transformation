"""ProPainter 视频修复引擎封装（时序视频修复，解决逐帧 TELEA 在运动主体上的涂抹）。

依赖：
  * pip 包 ``propainter``（0.0.4，底层流式迭代器）
  * ``pytorchcv``（ProPainter / RAFT 模型与权重）
  * CUDA GPU + 模型权重（首次运行自动下载，约数百 MB）

``inpaint_video`` 把源视频帧与 ``box`` 遮罩落盘为图片，经
``ScaledProPainterIterator`` 做时序修复后写回视频（保留音轨）。
任意依赖 / 权重 / 算力缺失时抛 :class:`PropainterUnavailable`，由
``visual_cleanup.run_multipass_cleanup`` 捕获并降级到 sttn。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import cv2
import numpy as np


class PropainterUnavailable(Exception):
    """ProPainter 不可用（缺依赖 / 权重 / GPU），应降级到其它引擎。"""


def _dump_frames_and_masks(src: str, box: dict, frames_dir: Path, masks_dir: Path):
    """读源视频帧与 box 遮罩，落盘为 PNG，返回 (帧路径列表, 遮罩路径列表)。"""
    cap = cv2.VideoCapture(str(src))
    if not cap.isOpened():
        raise PropainterUnavailable(f"无法打开源视频: {src}")
    x, y, w, h = int(box["x"]), int(box["y"]), int(box["w"]), int(box["h"])
    frame_paths, mask_paths = [], []
    idx = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frames_dir.mkdir(parents=True, exist_ok=True)
        masks_dir.mkdir(parents=True, exist_ok=True)
        fp = frames_dir / f"f{idx:06d}.png"
        cv2.imwrite(str(fp), frame)
        mask = np.zeros(frame.shape[:2], np.uint8)
        mask[y:y + h, x:x + w] = 255
        mp = masks_dir / f"m{idx:06d}.png"
        cv2.imwrite(str(mp), mask)
        frame_paths.append(str(fp))
        mask_paths.append(str(mp))
        idx += 1
    cap.release()
    if idx == 0:
        raise PropainterUnavailable("源视频无帧")
    return frame_paths, mask_paths


def _write_frames_with_audio(src: str, dest: str, frames_rgb: np.ndarray):
    """把 RGB 帧序列写回视频并复用源音轨。"""
    if frames_rgb.ndim != 4 or frames_rgb.shape[-1] != 3:
        raise PropainterUnavailable(f"ProPainter 输出形状异常: {frames_rgb.shape}")
    h, w = frames_rgb.shape[1], frames_rgb.shape[2]
    tmp = tempfile.mktemp(suffix=".avi")
    vw = cv2.VideoWriter(tmp, cv2.VideoWriter_fourcc(*"MJPG"), 30, (w, h))
    for f in frames_rgb:
        vw.write(cv2.cvtColor(f, cv2.COLOR_RGB2BGR))
    vw.release()

    try:
        import imageio_ffmpeg
        ff = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        ff = "ffmpeg"
    cmd = [
        ff, "-y", "-i", tmp, "-i", str(src),
        "-map", "0:v:0", "-map", "1:a?",
        "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "copy", str(dest),
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    finally:
        Path(tmp).unlink(missing_ok=True)


def inpaint_video(src, dest, box, *, width=None, height=None):
    """用 ProPainter 时序修复 ``box`` 区域。失败一律抛 PropainterUnavailable。"""
    try:
        import torch
        if not torch.cuda.is_available():
            raise PropainterUnavailable("ProPainter 需要 CUDA GPU；当前无可用 GPU")
        from propainter_video import (  # type: ignore
            ScaledProPainterIterator, RawFrameSequencer, RawMaskSequencer,
            run_streaming_propainter,
        )
    except Exception as e:  # 缺 pip 包 / 无 GPU
        raise PropainterUnavailable(f"ProPainter 依赖不可用: {e}") from e

    work = Path(tempfile.mkdtemp(prefix="propainter_"))
    frames_dir = work / "frames"
    masks_dir = work / "masks"
    try:
        frame_paths, mask_paths = _dump_frames_and_masks(str(src), box, frames_dir, masks_dir)
        try:
            raw_frames = RawFrameSequencer(data=frame_paths)
            raw_masks = RawMaskSequencer(data=mask_paths)
            iterator = ScaledProPainterIterator(
                raw_frames=raw_frames,
                raw_masks=raw_masks,
                image_resize_ratio=1.0,
                mask_dilation=4,
            )
            out_frames = run_streaming_propainter(iterator)  # (T,H,W,3) RGB uint8
        except Exception as e:  # 权重缺失 / 模型加载失败 / API 不匹配
            raise PropainterUnavailable(
                f"ProPainter 模型加载或推理失败（需权重+GPU）: {e}"
            ) from e
        _write_frames_with_audio(str(src), str(dest), out_frames)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return {"engine": "propainter", "frames": len(frame_paths)}
