"""Download local vision weights for dehardsub / demosaic / deblur.

Usage (optional SOCKS proxy, as used for HF in China):
  set ALL_PROXY=socks5h://127.0.0.1:10808
  .venv\\Scripts\\python scripts\\download_vision_models.py

Or: curl --socks5-hostname 127.0.0.1:10808 -L -o models\\lama.onnx URL
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "models"

# (url, dest_name, min_bytes)
ARTIFACTS = [
    (
        "https://huggingface.co/Carve/LaMa-ONNX/resolve/main/lama_fp32.onnx",
        "lama.onnx",
        50_000_000,
    ),
    (
        "https://huggingface.co/opencv/inpainting_lama/resolve/main/inpainting_lama_2025jan.onnx",
        "lama_opencv.onnx",
        5_000_000,
    ),
]


def _download(url: str, dest: Path, min_bytes: int) -> None:
    if dest.is_file() and dest.stat().st_size >= min_bytes:
        print(f"skip {dest.name} ({dest.stat().st_size} bytes)")
        return
    proxy = (
        os.environ.get("ALL_PROXY")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("HTTP_PROXY")
        or ""
    ).strip()
    print(f"GET {url}\n -> {dest}  proxy={proxy or 'direct'}")
    try:
        import requests
    except ImportError as exc:
        raise SystemExit("pip install requests PySocks") from exc
    proxies = {"http": proxy, "https": proxy} if proxy else None
    with requests.get(url, proxies=proxies, stream=True, timeout=180) as resp:
        resp.raise_for_status()
        tmp = dest.with_suffix(dest.suffix + ".part")
        n = 0
        with open(tmp, "wb") as fh:
            for chunk in resp.iter_content(1024 * 1024):
                if not chunk:
                    continue
                fh.write(chunk)
                n += len(chunk)
        tmp.replace(dest)
    if dest.stat().st_size < min_bytes:
        raise RuntimeError(f"{dest} too small ({dest.stat().st_size}); likely an error page")
    print(f"OK {dest.name} {dest.stat().st_size}")


def main() -> int:
    MODELS.mkdir(parents=True, exist_ok=True)
    failed = 0
    for url, name, min_b in ARTIFACTS:
        try:
            _download(url, MODELS / name, min_b)
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL {name}: {exc}", file=sys.stderr)
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
