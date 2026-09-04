"""Job Pack: zip a pack work_dir for download (WF-10 / Phase F light).

Includes ``subs/``, ``notes/``, selected ``media/*`` products, and ``pack_manifest.json``.
"""

from __future__ import annotations

import io
import json
import time
import zipfile
from pathlib import Path
from typing import Any, BinaryIO

# Paths relative to work_dir that belong in a Job Pack.
PACK_ROOT_DIRS = ("subs", "notes")
PACK_MEDIA_DIRS = (
    "dehardsub",
    "deblur",
    "enhance",
    "compress",
    "concat",
    "clips",
    "remix",
    "publish",
    "frames",
)
PACK_MEDIA_FILES = (
    "pack_manifest.json",
    "media_status.json",
    "fetch_meta.json",
    "sync_meta.json",
)


def safe_resolve_under(work_dir: Path, rel: str) -> Path:
    """Resolve ``rel`` under work_dir; raise ValueError on traversal / missing."""
    root = Path(work_dir).resolve()
    raw = (rel or "").replace("\\", "/").lstrip("/")
    if not raw or ".." in raw.split("/"):
        raise ValueError("invalid path")
    target = (root / raw).resolve()
    try:
        target.relative_to(root)
    except ValueError as e:
        raise ValueError("path escapes work_dir") from e
    if not target.is_file():
        raise FileNotFoundError(raw)
    return target


def _iter_pack_files(work_dir: Path) -> list[tuple[str, Path]]:
    """Return (arcname, path) pairs for the zip."""
    root = Path(work_dir)
    out: list[tuple[str, Path]] = []
    seen: set[str] = set()

    def add(path: Path, arc: str) -> None:
        key = arc.replace("\\", "/")
        if key in seen or not path.is_file():
            return
        if path.stat().st_size <= 0:
            return
        seen.add(key)
        out.append((key, path))

    for name in PACK_ROOT_DIRS:
        d = root / name
        if not d.is_dir():
            continue
        for p in d.rglob("*"):
            if p.is_file():
                add(p, str(p.relative_to(root)).replace("\\", "/"))

    media = root / "media"
    if media.is_dir():
        for name in PACK_MEDIA_FILES:
            add(media / name, f"media/{name}")
        for sub in PACK_MEDIA_DIRS:
            d = media / sub
            if not d.is_dir():
                continue
            for p in d.rglob("*"):
                if p.is_file():
                    add(p, str(p.relative_to(root)).replace("\\", "/"))
        # Optional source preview (small enough? skip full source by default — too large)
        # Include source only if < 80MB
        for src_name in ("source.mp4", "source.mkv", "source.webm"):
            sp = media / src_name
            if sp.is_file() and sp.stat().st_size < 80 * 1024 * 1024:
                add(sp, f"media/{src_name}")
                break

    return out


def build_job_pack_bytes(
    work_dir: Path,
    *,
    pack_id: str | None = None,
    meta_extra: dict[str, Any] | None = None,
) -> bytes:
    """Build an in-memory Job Pack zip."""
    root = Path(work_dir)
    if not root.is_dir():
        raise FileNotFoundError(str(root))
    files = _iter_pack_files(root)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        meta = {
            "pack_id": pack_id or root.name,
            "created_at": time.time(),
            "files": [arc for arc, _ in files],
            **(meta_extra or {}),
        }
        zf.writestr("meta.json", json.dumps(meta, ensure_ascii=False, indent=2))
        for arc, path in files:
            zf.write(path, arcname=arc)
    return buf.getvalue()


def write_job_pack_zip(
    work_dir: Path,
    dest: Path | BinaryIO,
    *,
    pack_id: str | None = None,
    meta_extra: dict[str, Any] | None = None,
) -> int:
    """Write zip to path or file object; return byte size."""
    data = build_job_pack_bytes(work_dir, pack_id=pack_id, meta_extra=meta_extra)
    if hasattr(dest, "write"):
        dest.write(data)  # type: ignore[union-attr]
    else:
        Path(dest).write_bytes(data)
    return len(data)
