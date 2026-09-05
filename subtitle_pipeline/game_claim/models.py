"""Typed models for store offer discovery and claim results."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

ClaimStatus = Literal["claimed", "owned", "skipped", "manual", "failed"]


@dataclass(slots=True)
class StoreOffer:
    """One store item that might be claimable."""

    store: str
    title: str
    url: str
    source_url: str
    claimable: bool = True
    notes: str = ""


@dataclass(slots=True)
class ClaimRecord:
    """Outcome for one item."""

    store: str
    title: str
    url: str
    status: ClaimStatus
    message: str = ""
    screenshot: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class ClaimSummary:
    """Full run summary written to disk and returned to callers."""

    started_at: str
    finished_at: str
    dry_run: bool
    headless: bool
    stores: list[str]
    output_dir: Path
    results: list[ClaimRecord] = field(default_factory=list)

    @property
    def claimed(self) -> int:
        return sum(1 for item in self.results if item.status == "claimed")

    @property
    def owned(self) -> int:
        return sum(1 for item in self.results if item.status == "owned")

    @property
    def skipped(self) -> int:
        return sum(1 for item in self.results if item.status == "skipped")

    @property
    def manual(self) -> int:
        return sum(1 for item in self.results if item.status == "manual")

    @property
    def failed(self) -> int:
        return sum(1 for item in self.results if item.status == "failed")

    @property
    def ok(self) -> bool:
        return self.failed == 0 and self.manual == 0

    @property
    def exit_code(self) -> int:
        if self.failed:
            return 1
        if self.manual:
            return 2
        return 0

    def to_dict(self) -> dict:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "dry_run": self.dry_run,
            "headless": self.headless,
            "stores": self.stores,
            "output_dir": str(self.output_dir),
            "claimed": self.claimed,
            "owned": self.owned,
            "skipped": self.skipped,
            "manual": self.manual,
            "failed": self.failed,
            "ok": self.ok,
            "exit_code": self.exit_code,
            "results": [item.to_dict() for item in self.results],
        }
