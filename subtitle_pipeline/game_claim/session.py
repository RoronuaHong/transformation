"""Playwright persistent-session helpers."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Playwright


class GameClaimRuntimeError(RuntimeError):
    """Raised when browser automation prerequisites are missing."""


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def persistent_context(
    profile_dir: Path,
    *,
    headless: bool,
    slow_mo_ms: int = 0,
    browser_channel: str = "chromium",
    profile_name: str = "",
) -> Iterator["BrowserContext"]:
    """Launch a persistent Chromium context and shut it down safely."""

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise GameClaimRuntimeError(
            "Playwright is not installed. Run `pip install playwright` and "
            "`python -m playwright install chromium`."
        ) from exc

    ensure_dir(profile_dir)
    playwright: Playwright = sync_playwright().start()
    args = ["--disable-blink-features=AutomationControlled"]
    if profile_name.strip():
        args.append(f"--profile-directory={profile_name.strip()}")
    channel = browser_channel.strip().lower()
    context = playwright.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir),
        headless=headless,
        slow_mo=slow_mo_ms,
        args=args,
        **({"channel": channel} if channel and channel != "chromium" else {}),
    )
    try:
        yield context
    finally:
        context.close()
        playwright.stop()
