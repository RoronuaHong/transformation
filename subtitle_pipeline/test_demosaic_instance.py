"""实例测试：合成「硬字幕条 + 马赛克块」视频，验证去马赛克路线与顺序修正。

产物目录：subtitle_pipeline/instances/demosaic_test/
  - source.mp4               合成样片（底部白字幕条 + 中部棋盘格马赛克）
  - out_opencv.mp4          去马赛克=OpenCV 兜底，先修马赛克→再 STTN 去字幕（新顺序）
  - out_codeformer.mp4       去马赛克=CodeFormer（无权重→自动降级 OpenCV），先修→再 STTN
  - out_sttn_combined.mp4    旧合一路径：STTN 同时把字幕+马赛克当洞补（对比用）
  - metrics.json            马赛克区方差(越低=修复越平滑) + 字幕残差等指标

说明：修正点——之前默认顺序是「STTN 先去字幕 → OpenCV 再去马赛克」，
STTN 字幕权重会把马赛克区补糊；现改为「马赛克先在原始像素上修 → 再 STTN 去字幕」。
"""
from pathlib import Path

import cv2
import numpy as np

INSTANCES = Path(__file__).resolve().parent / "instances" / "demosaic_test"
BW, BH, CELL = 80, 80, 16
BX, BY = (320 - BW) // 2, (240 - BH) // 2 - 30  # 马赛克块位置


def _make_sample(path: Path) -> None:
    w, h, fps, n = 320, 240, 10, 15
    INSTANCES.mkdir(parents=True, exist_ok=True)
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(path), fourcc, fps, (w, h))
    assert writer.isOpened(), "cv2 writer open failed"
    grid = np.zeros((BH, BW, 3), dtype=np.uint8)
    for gy in range(0, BH, CELL):
        for gx in range(0, BW, CELL):
            c = 80 + ((gx // CELL + gy // CELL) % 2) * 70  # 棋盘格 80/150
            grid[gy : gy + CELL, gx : gx + CELL] = (c, c, c)
    for i in range(n):
        frame = np.full((h, w, 3), 40, dtype=np.uint8)
        frame[int(h * 0.82) :, :] = (235, 235, 235)
        cv2.putText(frame, "HELLO SUB", (60, 216), cv2.FONT_HERSHEY_SIMPLEX,
                    0.7, (20, 20, 20), 2, cv2.LINE_AA)
        jitter = ((i * 7) % 2) * 6
        frame[BY : BY + BH, BX : BX + BW] = np.clip(grid.astype(int) + jitter, 0, 255).astype(np.uint8)
        writer.write(frame)
    writer.release()


def _mosaic_pixels(video: Path) -> np.ndarray:
    cap = cv2.VideoCapture(str(video))
    frames = []
    while True:
        ok, f = cap.read()
        if not ok:
            break
        frames.append(f[BY : BY + BH, BX : BX + BW].astype(np.float32))
    cap.release()
    return np.stack(frames) if frames else np.zeros((1, 1, 1, 3))


def _mosaic_variance(video: Path) -> float:
    """马赛克区逐帧平均方差（越低=块越平滑=修复越好）。"""
    px = _mosaic_pixels(video)
    if px.ndim != 4:
        return -1.0
    return float(px.reshape(px.shape[0], -1).var(axis=1).mean())


def _hardsub_residual(video: Path, box: dict[str, int]) -> float:
    """白底字幕带上的暗字残留（越低=字幕擦得越干净）。

    字幕带背景≈235；以 |像素-235| 的均值度量暗字残差（源含暗字→高，修复后→低）。
    """
    cap = cv2.VideoCapture(str(video))
    frames = []
    while True:
        ok, f = cap.read()
        if not ok:
            break
        frames.append(f[box["y"] : box["y"] + box["h"], box["x"] : box["x"] + box["w"]].astype(np.float32))
    cap.release()
    if not frames:
        return -1.0
    stack = np.stack(frames)
    return float(np.mean(np.abs(stack - 235.0)))


def test_demosaic_instance_pipeline() -> None:
    from visual_cleanup import mosaic_block_score, run_multipass_cleanup

    INSTANCES.mkdir(parents=True, exist_ok=True)
    src = INSTANCES / "source.mp4"
    _make_sample(src)

    # 合成马赛克是有效棋盘格（检测器阈值之上的图案）
    cap = cv2.VideoCapture(str(src))
    ok, frame = cap.read()
    cap.release()
    assert ok
    roi = frame[BY : BY + BH, BX : BX + BW]
    score = mosaic_block_score(cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY))
    assert score > 0.5, f"synthetic mosaic pattern too weak (score={score})"

    # 字幕带：白底区域（h*0.82≈197 起），聚焦纯白底以避免灰底污染指标
    box = {"x": 35, "y": 197, "w": 248, "h": 42}
    mosaic_box = {"x": BX, "y": BY, "w": BW, "h": BH}

    metrics: dict[str, object] = {
        "source": str(src),
        "mosaic_box": mosaic_box,
        "mosaic_block_score": round(score, 3),
        "src_mosaic_variance": round(_mosaic_variance(src), 1),
        "src_hardsub_residual": round(_hardsub_residual(src, box), 1),
    }

    base = dict(
        locate_mode="band", engine="sttn", demosaic=True, dehardsub=True,
    )
    # 显式马赛克框（合成样片自动检测偏保守，真实场景也可手动标注）
    mb = dict(mosaic_boxes=[mosaic_box])

    # A) 新顺序：OpenCV 兜底先修马赛克 → 再 STTN 去字幕
    out_cv = INSTANCES / "out_opencv.mp4"
    run_multipass_cleanup(src, out_cv, work_dir=INSTANCES / "work_opencv",
                          demosaic_engine="opencv", **base, **mb)
    assert out_cv.is_file()

    # B) 新顺序：CodeFormer 先修马赛克（无权重→自动降级 OpenCV）→ 再 STTN
    out_cf = INSTANCES / "out_codeformer.mp4"
    run_multipass_cleanup(src, out_cf, work_dir=INSTANCES / "work_codeformer",
                          demosaic_engine="codeformer", **base, **mb)
    assert out_cf.is_file()

    # C) 旧合一路径对比：STTN 同时把字幕+马赛克当洞补（质量一般）
    out_sttn = INSTANCES / "out_sttn_combined.mp4"
    run_multipass_cleanup(src, out_sttn, work_dir=INSTANCES / "work_sttn",
                          demosaic_engine="sttn", **base, **mb)
    assert out_sttn.is_file()

    orig = _mosaic_pixels(src)
    variants = {}
    for name, out in (("opencv", out_cv), ("codeformer", out_cf), ("sttn_combined", out_sttn)):
        o = _mosaic_pixels(out)
        change = float(np.mean(np.abs(o - orig))) if o.shape == orig.shape else -1.0
        variants[name] = {
            "out": str(out),
            "mosaic_pixel_change": round(change, 2),
            "mosaic_variance": round(_mosaic_variance(out), 1),
            "hardsub_residual": round(_hardsub_residual(out, box), 1),
        }
    metrics["variants"] = variants
    (INSTANCES / "metrics.json").write_text(
        __import__("json").dumps(metrics, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # 新顺序下马赛克区必须被改变（证明先修马赛克生效），且方差下降（被平滑修复）
    assert variants["opencv"]["mosaic_pixel_change"] > 1.0
    assert variants["opencv"]["mosaic_variance"] < metrics["src_mosaic_variance"]
    # 字幕应被擦掉（白底暗字残差下降；STTN 擦除质量由其它测试覆盖）
    assert variants["opencv"]["hardsub_residual"] < metrics["src_hardsub_residual"]
    # CodeFormer 路径在无权重时应优雅降级且产物与其 OpenCV 版一致（不崩）
    assert variants["codeformer"]["mosaic_pixel_change"] > 1.0
