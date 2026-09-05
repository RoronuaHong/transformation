from __future__ import annotations

from pathlib import Path

from api.settings import Settings
from game_claim.models import ClaimRecord, ClaimSummary
from game_claim.runner import build_config_from_settings
from game_claim.stores import classify_epic_cta, classify_steam_page, parse_store_csv, parse_url_csv


def test_classify_epic_cta_distinguishes_owned_and_claimable() -> None:
    assert classify_epic_cta("In Library") == "owned"
    assert classify_epic_cta("Get") == "claimable"
    assert classify_epic_cta("Wishlist") == "unknown"


def test_classify_steam_page_filters_install_only_freebies() -> None:
    assert classify_steam_page("Add to Account") == "claimable"
    assert classify_steam_page("Already in your Steam library") == "owned"
    assert classify_steam_page("Install Game") == "unsupported"


def test_parse_helpers_dedupe_and_ignore_unknown_entries() -> None:
    assert parse_store_csv("epic, steam, epic, gog") == ["epic", "steam"]
    assert parse_url_csv("https://a, https://b, https://a") == ["https://a", "https://b"]


def test_build_config_from_settings_uses_game_claim_fields() -> None:
    settings = Settings(
        game_claim_stores="steam",
        game_claim_profile_dir="profiles",
        game_claim_artifacts_dir="runs",
        game_claim_headless=False,
        game_claim_slow_mo_ms=250,
        game_claim_browser_channel="msedge",
        game_claim_profile_name="Default",
        game_claim_epic_urls="https://store.epicgames.com/en-US/p/demo",
        game_claim_steam_urls="https://store.steampowered.com/app/123/demo/",
    )
    cfg = build_config_from_settings(settings)
    assert cfg.stores == ["steam"]
    assert cfg.profile_dir == Path("profiles")
    assert cfg.artifacts_dir == Path("runs")
    assert cfg.headless is False
    assert cfg.slow_mo_ms == 250
    assert cfg.browser_channel == "msedge"
    assert cfg.profile_name == "Default"
    assert cfg.epic_urls == ["https://store.epicgames.com/en-US/p/demo"]
    assert cfg.steam_urls == ["https://store.steampowered.com/app/123/demo/"]


def test_claim_summary_counts_and_exit_code() -> None:
    summary = ClaimSummary(
        started_at="2026-01-01T00:00:00Z",
        finished_at="2026-01-01T00:00:01Z",
        dry_run=False,
        headless=True,
        stores=["epic", "steam"],
        output_dir=Path("out"),
        results=[
            ClaimRecord("epic", "A", "u1", "claimed"),
            ClaimRecord("epic", "B", "u2", "owned"),
            ClaimRecord("steam", "C", "u3", "manual"),
        ],
    )
    assert summary.claimed == 1
    assert summary.owned == 1
    assert summary.manual == 1
    assert summary.failed == 0
    assert summary.exit_code == 2
