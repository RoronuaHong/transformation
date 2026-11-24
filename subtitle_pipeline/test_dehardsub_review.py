"""逐帧审阅视频与风险排序的回归测试（纯合成，不依赖具体视频）。"""
from __future__ import annotations

import csv
import json
import os
import sys

import cv2
import numpy as np

ROOT = os.path.dirname(__file__)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from dehardsub_review import build_review_pair, score_risk  # noqa: E402


def _write_video(path, frames):
    h, w = frames[0].shape[:2]
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 30, (w, h))
    for f in frames:
        vw.write(f)
    vw.release()


def _sample_frames(n=6):
    h, w = 90, 160
    src, clean = [], []
    for i in range(n):
        base = np.full((h, w, 3), 40 + i, np.uint8)
        cv2.putText(base, "WM", (8, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                    (210, 210, 210), 2, cv2.LINE_AA)
        out = base.copy()
        out[18:32, 6:60] = 40 + i
        src.append(base)
        clean.append(out)
    return src, clean


def test_build_review_pair_is_frame_accurate(tmp_path):
    src_frames, clean_frames = _sample_frames()
    src, clean, out = tmp_path / "src.avi", tmp_path / "clean.avi", tmp_path / "review.mp4"
    _write_video(src, src_frames)
    _write_video(clean, clean_frames)
    stats = build_review_pair(src, clean, out)
    assert stats["written_frames"] == 6
    assert stats["aligned"] is True
    cap = cv2.VideoCapture(str(out))
    n = 0
    while True:
        ok, f = cap.read()
        if not ok:
            break
        assert f.shape[1] == 320
        n += 1
    cap.release()
    assert n == 6


def test_score_risk_writes_relative_ranking(tmp_path):
    src_frames, clean_frames = _sample_frames()
    src, clean = tmp_path / "src.avi", tmp_path / "clean.avi"
    _write_video(src, src_frames)
    _write_video(clean, clean_frames)
    regions = tmp_path / "regions.json"
    regions.write_text(json.dumps({"boxes": [
        {"x": 0, "y": 60, "w": 160, "h": 30, "role": "dialogue"},
        {"x": 0, "y": 10, "w": 70, "h": 30, "role": "up_watermark"},
    ]}), encoding="utf-8")
    csv_path, json_path = tmp_path / "risk.csv", tmp_path / "risk.json"
    stats = score_risk(src, clean, regions, out_csv=csv_path, out_json=json_path, workers=1)
    assert stats["frames_scored"] == 6
    with csv_path.open(encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 6
    assert "risk_score" in rows[0]
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert "not an objective quality score" in payload["note"]
    assert len(payload["worst30"]) == 6
