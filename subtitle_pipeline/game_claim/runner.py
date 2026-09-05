"""CLI entrypoint for weekly free-game claim jobs."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from game_claim.models import ClaimRecord, ClaimSummary
from game_claim.session import GameClaimRuntimeError, ensure_dir, persistent_context
from game_claim.stores import get_claimer, parse_store_csv, parse_url_csv

if TYPE_CHECKING:
    from api.settings import Settings

LOGGER = logging.getLogger("game_claim")


@dataclass(slots=True)
class ClaimConfig:
    """Runtime config for one game-claim run."""

    stores: list[str]
    profile_dir: Path
    artifacts_dir: Path
    headless: bool = True
    dry_run: bool = False
    slow_mo_ms: int = 0
    browser_channel: str = "chromium"
    profile_name: str = ""
    epic_urls: list[str] = field(default_factory=list)
    steam_urls: list[str] = field(default_factory=list)


def default_config() -> ClaimConfig:
    root = Path("data") / "game_claim"
    return ClaimConfig(
        stores=["epic", "steam"],
        profile_dir=root / "browser",
        artifacts_dir=root / "runs",
    )


def build_config_from_settings(
    settings: "Settings",
    *,
    stores: list[str] | None = None,
    headless: bool | None = None,
    dry_run: bool = False,
    epic_urls: list[str] | None = None,
    steam_urls: list[str] | None = None,
) -> ClaimConfig:
    setting_stores = parse_store_csv(settings.game_claim_stores) or ["epic", "steam"]
    return ClaimConfig(
        stores=stores or setting_stores,
        profile_dir=Path(settings.game_claim_profile_dir),
        artifacts_dir=Path(settings.game_claim_artifacts_dir),
        headless=settings.game_claim_headless if headless is None else headless,
        dry_run=dry_run,
        slow_mo_ms=max(0, int(settings.game_claim_slow_mo_ms)),
        browser_channel=settings.game_claim_browser_channel,
        profile_name=settings.game_claim_profile_name,
        epic_urls=epic_urls if epic_urls is not None else parse_url_csv(settings.game_claim_epic_urls),
        steam_urls=steam_urls if steam_urls is not None else parse_url_csv(settings.game_claim_steam_urls),
    )


def _run_output_dir(base_dir: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return ensure_dir(base_dir / stamp)


def login(store: str, config: ClaimConfig) -> int:
    """Open a persistent browser window so the user can sign in once."""

    claimer = get_claimer(store)
    profile_dir = (
        ensure_dir(config.profile_dir / claimer.store)
        if not config.profile_name
        else ensure_dir(config.profile_dir)
    )
    with persistent_context(
        profile_dir,
        headless=False,
        slow_mo_ms=config.slow_mo_ms,
        browser_channel=config.browser_channel,
        profile_name=config.profile_name,
    ) as context:
        claimer.open_login(context)
        print(
            f"Log into {claimer.store} in the opened browser window, then close that window "
            "to save the profile and finish setup."
        )
        while context.pages:
            time.sleep(1)
    return 0


def _manual_urls(config: ClaimConfig, store: str) -> list[str] | None:
    if store == "epic":
        return config.epic_urls or None
    if store == "steam":
        return config.steam_urls or None
    return None


def run_claims(config: ClaimConfig) -> ClaimSummary:
    """Execute discovery and claim flows for all requested stores."""

    started_at = datetime.now(UTC).isoformat()
    output_dir = _run_output_dir(config.artifacts_dir)
    summary = ClaimSummary(
        started_at=started_at,
        finished_at=started_at,
        dry_run=config.dry_run,
        headless=config.headless,
        stores=config.stores,
        output_dir=output_dir,
    )
    ensure_dir(config.profile_dir)

    for store in config.stores:
        claimer = get_claimer(store)
        profile_dir = (
            ensure_dir(config.profile_dir / claimer.store)
            if not config.profile_name
            else ensure_dir(config.profile_dir)
        )
        try:
            with persistent_context(
                profile_dir,
                headless=config.headless,
                slow_mo_ms=config.slow_mo_ms,
                browser_channel=config.browser_channel,
                profile_name=config.profile_name,
            ) as context:
                offers = claimer.discover(context, manual_urls=_manual_urls(config, store))
                if not offers:
                    summary.results.append(
                        ClaimRecord(
                            store=store,
                            title=f"{store}-discover",
                            url="",
                            status="skipped",
                            message="no claimable offers discovered",
                            screenshot=None,
                        )
                    )
                    continue
                for offer in offers:
                    summary.results.append(
                        claimer.claim(
                            context,
                            offer,
                            dry_run=config.dry_run,
                            output_dir=output_dir,
                        )
                    )
        except GameClaimRuntimeError:
            raise
        except Exception as exc:
            summary.results.append(
                ClaimRecord(
                    store=store,
                    title=f"{store}-run",
                    url="",
                    status="failed",
                    message=str(exc),
                    screenshot=None,
                )
            )

    summary.finished_at = datetime.now(UTC).isoformat()
    (output_dir / "summary.json").write_text(
        json.dumps(summary.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return summary


def run_from_settings(
    settings: "Settings",
    *,
    stores: list[str] | None = None,
    headless: bool | None = None,
    dry_run: bool = False,
    epic_urls: list[str] | None = None,
    steam_urls: list[str] | None = None,
) -> dict:
    config = build_config_from_settings(
        settings,
        stores=stores,
        headless=headless,
        dry_run=dry_run,
        epic_urls=epic_urls,
        steam_urls=steam_urls,
    )
    summary = run_claims(config)
    out = summary.to_dict()
    out["summary_path"] = str(summary.output_dir / "summary.json")
    return out


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Claim weekly free Epic/Steam games")
    sub = parser.add_subparsers(dest="command")

    login_parser = sub.add_parser("login", help="Open a persistent browser for first login")
    login_parser.add_argument("--store", required=True, choices=["epic", "steam"])
    login_parser.add_argument("--profile-dir", default=str(default_config().profile_dir))
    login_parser.add_argument("--slow-mo-ms", type=int, default=0)
    login_parser.add_argument("--browser-channel", default="chromium")
    login_parser.add_argument("--profile-name", default="")

    run_parser = sub.add_parser("run", help="Discover and claim free games")
    run_parser.add_argument("--stores", default="epic,steam")
    run_parser.add_argument("--profile-dir", default=str(default_config().profile_dir))
    run_parser.add_argument("--artifacts-dir", default=str(default_config().artifacts_dir))
    run_parser.add_argument("--headed", action="store_true")
    run_parser.add_argument("--dry-run", action="store_true")
    run_parser.add_argument("--slow-mo-ms", type=int, default=0)
    run_parser.add_argument("--browser-channel", default="chromium")
    run_parser.add_argument("--profile-name", default="")
    run_parser.add_argument("--epic-url", action="append", default=[])
    run_parser.add_argument("--steam-url", action="append", default=[])

    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = build_parser()
    args = parser.parse_args(argv)

    command = args.command or "run"
    if command == "login":
        cfg = ClaimConfig(
            stores=[args.store],
            profile_dir=Path(args.profile_dir),
            artifacts_dir=default_config().artifacts_dir,
            headless=False,
            slow_mo_ms=max(0, int(args.slow_mo_ms)),
            browser_channel=str(args.browser_channel or "chromium"),
            profile_name=str(args.profile_name or "").strip(),
        )
        return login(args.store, cfg)

    config = ClaimConfig(
        stores=parse_store_csv(args.stores) or ["epic", "steam"],
        profile_dir=Path(args.profile_dir),
        artifacts_dir=Path(args.artifacts_dir),
        headless=not bool(args.headed),
        dry_run=bool(args.dry_run),
        slow_mo_ms=max(0, int(args.slow_mo_ms)),
        browser_channel=str(args.browser_channel or "chromium"),
        profile_name=str(args.profile_name or "").strip(),
        epic_urls=args.epic_url,
        steam_urls=args.steam_url,
    )
    try:
        summary = run_claims(config)
    except GameClaimRuntimeError as exc:
        LOGGER.error("%s", exc)
        return 1

    print(json.dumps(summary.to_dict(), ensure_ascii=False, indent=2))
    return summary.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
