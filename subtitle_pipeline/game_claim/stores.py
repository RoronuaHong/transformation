"""Store-specific discovery and claim flows."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

from game_claim.models import ClaimRecord, StoreOffer

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Locator, Page


EPIC_FREE_GAMES_URL = "https://store.epicgames.com/en-US/free-games"
STEAM_SPECIALS_URL = "https://store.steampowered.com/search/?maxprice=free&specials=1"


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


def slugify(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return text or "offer"


def classify_epic_cta(label: str) -> str:
    text = normalize_text(label).lower()
    if not text:
        return "unknown"
    if any(token in text for token in ("in library", "owned", "already in library")):
        return "owned"
    if any(token in text for token in ("get", "place order", "order now", "checkout")):
        return "claimable"
    return "unknown"


def classify_steam_page(text: str) -> str:
    sample = normalize_text(text).lower()
    if not sample:
        return "unknown"
    if "add to account" in sample or "add to library" in sample:
        return "claimable"
    if any(token in sample for token in ("in library", "already in your steam library", "owned")):
        return "owned"
    if any(token in sample for token in ("play game", "install game", "download demo")):
        return "unsupported"
    return "unknown"


def parse_store_csv(value: str) -> list[str]:
    allowed = {"epic", "steam"}
    picked: list[str] = []
    for raw in value.split(","):
        item = raw.strip().lower()
        if item and item in allowed and item not in picked:
            picked.append(item)
    return picked


def parse_url_csv(value: str) -> list[str]:
    urls: list[str] = []
    for raw in value.split(","):
        item = raw.strip()
        if item and item not in urls:
            urls.append(item)
    return urls


def _capture(page: "Page", output_dir: Path, store: str, title: str, suffix: str) -> str:
    filename = f"{store}_{slugify(title)}_{suffix}.png"
    path = output_dir / filename
    page.screenshot(path=str(path), full_page=True)
    return str(path)


def _texts(locator: "Locator") -> list[str]:
    try:
        return [normalize_text(item) for item in locator.all_inner_texts() if normalize_text(item)]
    except Exception:
        return []


def _first_visible(locator: "Locator"):
    count = locator.count()
    for idx in range(count):
        item = locator.nth(idx)
        try:
            if item.is_visible():
                return item
        except Exception:
            continue
    return None


def _click_first_text(page: "Page", pattern: str) -> bool:
    locator = page.locator("button, a").filter(has_text=re.compile(pattern, re.I))
    try:
        count = locator.count()
    except Exception:
        return False
    for idx in range(count):
        item = locator.nth(idx)
        try:
            if item.is_visible():
                item.click(timeout=5000)
                return True
        except Exception:
            continue
    return False


class BaseClaimer(ABC):
    """Abstract store automation flow."""

    store: str
    login_url: str
    discover_url: str

    @abstractmethod
    def discover(
        self,
        context: "BrowserContext",
        *,
        manual_urls: list[str] | None = None,
    ) -> list[StoreOffer]:
        raise NotImplementedError

    @abstractmethod
    def claim(
        self,
        context: "BrowserContext",
        offer: StoreOffer,
        *,
        dry_run: bool,
        output_dir: Path,
    ) -> ClaimRecord:
        raise NotImplementedError

    def open_login(self, context: "BrowserContext") -> None:
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(self.login_url, wait_until="domcontentloaded")


class EpicClaimer(BaseClaimer):
    store = "epic"
    login_url = "https://store.epicgames.com/en-US/"
    discover_url = EPIC_FREE_GAMES_URL

    def discover(
        self,
        context: "BrowserContext",
        *,
        manual_urls: list[str] | None = None,
    ) -> list[StoreOffer]:
        if manual_urls:
            return [
                StoreOffer(
                    store=self.store,
                    title=urlparse(url).path.rstrip("/").split("/")[-1] or "epic-offer",
                    url=url,
                    source_url="manual",
                )
                for url in manual_urls
            ]

        page = context.pages[0] if context.pages else context.new_page()
        page.goto(self.discover_url, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        href_rows = page.locator('a[href*="/p/"]').evaluate_all(
            """els => els.map(el => ({
                href: el.href || "",
                text: (el.textContent || "").trim()
            }))"""
        )
        offers: list[StoreOffer] = []
        seen: set[str] = set()
        for row in href_rows:
            href = str(row.get("href") or "").strip()
            if not href or href in seen:
                continue
            seen.add(href)
            title = normalize_text(str(row.get("text") or ""))
            offers.append(
                StoreOffer(
                    store=self.store,
                    title=title or urlparse(href).path.rstrip("/").split("/")[-1] or "epic-offer",
                    url=href,
                    source_url=self.discover_url,
                )
            )
        return offers

    def claim(
        self,
        context: "BrowserContext",
        offer: StoreOffer,
        *,
        dry_run: bool,
        output_dir: Path,
    ) -> ClaimRecord:
        page = context.new_page()
        page.goto(offer.url, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        labels = _texts(page.locator("button, a, span"))
        state = next((classify_epic_cta(text) for text in labels if classify_epic_cta(text) != "unknown"), "unknown")

        if state == "owned":
            shot = _capture(page, output_dir, self.store, offer.title, "owned")
            return ClaimRecord(self.store, offer.title, offer.url, "owned", "already in library", shot)

        if dry_run:
            shot = _capture(page, output_dir, self.store, offer.title, "dry-run")
            return ClaimRecord(self.store, offer.title, offer.url, "skipped", "dry-run", shot)

        primary = _first_visible(page.locator("button, a"))
        if primary is None:
            shot = _capture(page, output_dir, self.store, offer.title, "manual")
            return ClaimRecord(self.store, offer.title, offer.url, "manual", "no clickable CTA found", shot)

        if not _click_first_text(page, "Get|Place Order|Order Now|Checkout"):
            shot = _capture(page, output_dir, self.store, offer.title, "manual")
            return ClaimRecord(self.store, offer.title, offer.url, "manual", "claim CTA not found", shot)
        page.wait_for_timeout(2000)
        try:
            if _click_first_text(page, "Place Order|Order"):
                page.wait_for_timeout(3500)
        except Exception:
            pass

        labels = _texts(page.locator("button, a, span"))
        state = next((classify_epic_cta(text) for text in labels if classify_epic_cta(text) != "unknown"), "unknown")
        if state == "owned":
            shot = _capture(page, output_dir, self.store, offer.title, "claimed")
            return ClaimRecord(self.store, offer.title, offer.url, "claimed", "claimed successfully", shot)

        shot = _capture(page, output_dir, self.store, offer.title, "manual")
        return ClaimRecord(
            self.store,
            offer.title,
            offer.url,
            "manual",
            "Epic checkout needs manual confirmation or selector refresh",
            shot,
        )


class SteamClaimer(BaseClaimer):
    store = "steam"
    login_url = "https://store.steampowered.com/login/"
    discover_url = STEAM_SPECIALS_URL

    def discover(
        self,
        context: "BrowserContext",
        *,
        manual_urls: list[str] | None = None,
    ) -> list[StoreOffer]:
        urls = manual_urls[:] if manual_urls else []
        if not urls:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(self.discover_url, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
            href_rows = page.locator('a[href*="store.steampowered.com/app/"], a[href*="store.steampowered.com/sub/"]').evaluate_all(
                """els => els.map(el => ({
                    href: el.href || "",
                    text: (el.textContent || "").trim()
                }))"""
            )
            for row in href_rows:
                href = str(row.get("href") or "").split("?")[0].strip()
                text = normalize_text(str(row.get("text") or ""))
                if not href:
                    continue
                if any(token in text for token in ("-100%", "100%", "free")) and href not in urls:
                    urls.append(href)
        offers: list[StoreOffer] = []
        for url in urls:
            title = urlparse(url).path.rstrip("/").split("/")[-1] or "steam-offer"
            offers.append(
                StoreOffer(
                    store=self.store,
                    title=title.replace("_", " "),
                    url=urljoin("https://store.steampowered.com", url),
                    source_url=self.discover_url if not manual_urls else "manual",
                )
            )
        return offers

    def claim(
        self,
        context: "BrowserContext",
        offer: StoreOffer,
        *,
        dry_run: bool,
        output_dir: Path,
    ) -> ClaimRecord:
        page = context.new_page()
        page.goto(offer.url, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        content = normalize_text(page.locator("body").inner_text(timeout=5000))
        state = classify_steam_page(content)

        if state == "owned":
            shot = _capture(page, output_dir, self.store, offer.title, "owned")
            return ClaimRecord(self.store, offer.title, offer.url, "owned", "already owned", shot)
        if state == "unsupported":
            shot = _capture(page, output_dir, self.store, offer.title, "skipped")
            return ClaimRecord(
                self.store,
                offer.title,
                offer.url,
                "skipped",
                "unsupported free type (demo / install-only / weekend)",
                shot,
            )
        if dry_run:
            shot = _capture(page, output_dir, self.store, offer.title, "dry-run")
            return ClaimRecord(self.store, offer.title, offer.url, "skipped", "dry-run", shot)

        try:
            add_button = page.locator("button, a").filter(
                has_text=re.compile("Add to Account|Add to Library", re.I)
            ).first
            if add_button.is_visible():
                add_button.click(timeout=5000)
                page.wait_for_timeout(2500)
                content = normalize_text(page.locator("body").inner_text(timeout=5000))
                state = classify_steam_page(content)
                if state == "owned":
                    shot = _capture(page, output_dir, self.store, offer.title, "claimed")
                    return ClaimRecord(self.store, offer.title, offer.url, "claimed", "claimed successfully", shot)
        except Exception:
            pass

        shot = _capture(page, output_dir, self.store, offer.title, "manual")
        return ClaimRecord(
            self.store,
            offer.title,
            offer.url,
            "manual",
            "Steam page needs manual confirmation or selector refresh",
            shot,
        )


def get_claimer(name: str) -> BaseClaimer:
    key = name.strip().lower()
    if key == "epic":
        return EpicClaimer()
    if key == "steam":
        return SteamClaimer()
    raise ValueError(f"unsupported store: {name}")
