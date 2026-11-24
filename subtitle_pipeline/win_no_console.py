"""Hide Windows console windows for child processes (ffmpeg / python / etc.).

PM2 already keeps long-lived services alive; flashing CMD comes from *child*
console-subsystem binaries spawned without CREATE_NO_WINDOW. Import this
module once at process start to patch ``subprocess.run`` / ``Popen``.
"""
from __future__ import annotations

import subprocess
import sys

_PATCHED = False


def apply() -> None:
    global _PATCHED
    if _PATCHED or sys.platform != "win32":
        return
    _PATCHED = True

    create_no_window = int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    startf_useshowwindow = int(getattr(subprocess, "STARTF_USESHOWWINDOW", 0x00000001))
    orig_run = subprocess.run
    orig_popen = subprocess.Popen

    def _inject(kwargs: dict) -> dict:
        flags = int(kwargs.get("creationflags") or 0) | create_no_window
        kwargs["creationflags"] = flags
        if "startupinfo" not in kwargs:
            si = subprocess.STARTUPINFO()
            si.dwFlags |= startf_useshowwindow
            si.wShowWindow = 0  # SW_HIDE
            kwargs["startupinfo"] = si
        return kwargs

    def run(*args, **kwargs):  # type: ignore[no-untyped-def]
        return orig_run(*args, **_inject(kwargs))

    class Popen(orig_popen):  # type: ignore[valid-type,misc]
        def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            super().__init__(*args, **_inject(kwargs))

    subprocess.run = run  # type: ignore[assignment]
    subprocess.Popen = Popen  # type: ignore[misc,assignment]


apply()
