"""E2E Stage 1 validation: genuinely download a real Bilibili source via fetch_media."""
import os
import sys
import json
from pathlib import Path

# Bilibili fetch is designed to go DIRECT (proxy breaks its TLS) — clear env proxies.
for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(k, None)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_media import download_bilibili_api_source, detect_bvid

URL = "https://www.bilibili.com/video/BV1LjWA6aEQ3"
OUT = Path(__file__).resolve().parent / "downloads" / "batch" / "bilibili_E2E_fetch" / "media" / "full_source.mp4"

print(f"[e2e-fetch] downloading {URL} -> {OUT}")
info = download_bilibili_api_source(URL, OUT)
print("[e2e-fetch] RESULT:")
print(json.dumps({k: v for k, v in info.items() if k != "source_file"}, ensure_ascii=False, indent=2))
print(f"[e2e-fetch] file exists={OUT.is_file()} size={OUT.stat().st_size if OUT.is_file() else 0}")
