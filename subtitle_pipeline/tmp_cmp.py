import pathlib, subprocess
import cv2, numpy as np
FF = r"D:\MineWeb\2026\Vitual\subtitle_pipeline\.venv\Lib\site-packages\imageio_ffmpeg\binaries\ffmpeg-win-x86_64-v7.1.exe"
M = pathlib.Path(r"D:\MineWeb\2026\Vitual\subtitle_pipeline\downloads\batch\bilibili_BV1oFJ16BEMT\media")
cap = cv2.VideoCapture(str(M / "source.mp4")); s = []
while True:
    ok, f = cap.read()
    if not ok: break
    s.append(f)
cap.release()
cap = cv2.VideoCapture(str(M / "dehardsub_full.mp4")); d = []
while True:
    ok, f = cap.read()
    if not ok: break
    d.append(f)
cap.release()
n = min(len(s), len(d))
gap = np.full((s[0].shape[0], 8, 3), 60, np.uint8)
fps = 30
tmp = M / "compare_full.avi"
vw = cv2.VideoWriter(str(tmp), cv2.VideoWriter_fourcc(*"MJPG"), fps, (s[0].shape[1]*2+8, s[0].shape[0]))
for k in range(n):
    vw.write(np.hstack([s[k], gap, d[k]]))
vw.release()
subprocess.run([FF,"-y","-i",str(tmp),"-an","-c:v","libx264","-crf","20","-pix_fmt","yuv420p",str(M/"compare_full.mp4")], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
tmp.unlink()
print("DONE compare_full.mp4", n, "frames", flush=True)
