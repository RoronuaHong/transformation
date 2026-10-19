"""临时看门狗：从 vitual_api.log 的 `[sttn] wrote X/Y` 实时同步进度文件。

仅用于当前这次 dehardsub 运行——老的 STTN 进程写入进度文件的逻辑有 bug
（%600 节流几乎不触发），但它每帧仍会在日志里打印真实帧进度。此脚本把
日志里的真实帧数同步到 .sttn_progress.json，让前端（读该文件）能实时显示。

代码修复（sttn_inpaint.py 每 flush 写文件 + 重试）上线（重启 API）后，此
看门狗不再需要，可结束。
"""
import json
import re
import time
from pathlib import Path

LOG = Path(r"D:\MineWeb\2026\Vitual\subtitle_pipeline\vitual_api.log")
OUT = Path(
    r"D:\MineWeb\2026\Vitual\subtitle_pipeline\downloads\batch\bilibili_BV1fRXxY1Eke\media\dehardsub\.sttn_progress.json"
)
# dehardsub 含多个顺序子步骤(STTN / edgefill / plate)，都往日志打 "X/Y"，统一抓取
PAT = re.compile(r"\[(sttn|edgefill:title|plate|cleanup)\][^\n]*?wrote (\d+)/(\d+)")
last = (-1, -1)

while True:
    try:
        txt = LOG.read_text(encoding="utf-8", errors="replace")
        m = None
        for m in PAT.finditer(txt):
            pass
        if m:
            f = int(m.group(2))
            t = int(m.group(3))
            if (f, t) != last:
                OUT.write_text(
                    json.dumps({"frames": f, "total": t, "ts": time.time()}),
                    encoding="utf-8",
                )
                last = (f, t)
                print(f"watchdog -> {f}/{t}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"watchdog err: {exc}", flush=True)
    time.sleep(3)
