"""WF-09 local publish-account slots (not download cookies, not git)."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PUBLISH_PLATFORMS = ("douyin", "kuaishou")
PLATFORM_DOMAINS = {
    "douyin": ("douyin.com", "iesdouyin.com", "douyinpic.com"),
    "kuaishou": ("kuaishou.com", "gifshow.com", "kwai.com"),
}
MIN_SECRET = 8

ROOT = Path(__file__).resolve().parent


class PublishAccountError(ValueError):
    """Invalid platform or secret for a publish slot."""


def accounts_path() -> Path:
    env = (os.environ.get("VITUAL_PUBLISH_ACCOUNTS") or "").strip()
    if env:
        return Path(env)
    return ROOT / "data" / "publish_accounts.json"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _load(path: Path | None = None) -> dict[str, Any]:
    p = path or accounts_path()
    if not p.is_file():
        return {"accounts": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {"accounts": []}
    if not isinstance(data, dict):
        return {"accounts": []}
    rows = data.get("accounts")
    if not isinstance(rows, list):
        data["accounts"] = []
    return data


def _save(data: dict[str, Any], path: Path | None = None) -> Path:
    p = path or accounts_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)
    return p


def normalize_platform(raw: str) -> str:
    p = (raw or "").strip().lower()
    aliases = {
        "dy": "douyin",
        "tiktok_cn": "douyin",
        "ks": "kuaishou",
        "kwai": "kuaishou",
    }
    p = aliases.get(p, p)
    if p not in PUBLISH_PLATFORMS:
        raise PublishAccountError(f"unknown publish platform: {raw}")
    return p


def _looks_netscape(secret: str) -> bool:
    for line in secret.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        return "\t" in line
    return False


def evaluate_secret(platform: str, secret: str) -> str:
    """Return valid or invalid. Empty is invalid."""
    text = (secret or "").strip()
    if len(text) < MIN_SECRET:
        return "invalid"
    if _looks_netscape(text):
        domains = PLATFORM_DOMAINS.get(platform, ())
        for line in text.splitlines():
            low = line.lower()
            if any(d in low for d in domains) and "\t" in line:
                return "valid"
        return "invalid"
    if re.search(r"sessionid\s*=", text, re.I):
        return "valid"
    return "valid"


def public_view(platform: str, rec: dict | None = None) -> dict[str, Any]:
    if not rec:
        return {
            "platform": platform,
            "label": "",
            "account_id": "",
            "status": "unbound",
            "bound": False,
            "secret_set": False,
            "updated_at": None,
        }
    return {
        "platform": platform,
        "label": str(rec.get("label") or ""),
        "account_id": str(rec.get("account_id") or ""),
        "status": str(rec.get("status") or "invalid"),
        "bound": True,
        "secret_set": bool(str(rec.get("secret") or "").strip()),
        "updated_at": rec.get("updated_at"),
    }


def list_slots(path: Path | None = None) -> list[dict[str, Any]]:
    data = _load(path)
    by_plat = {
        str(a.get("platform")): a
        for a in data.get("accounts") or []
        if isinstance(a, dict)
    }
    return [public_view(plat, by_plat.get(plat)) for plat in PUBLISH_PLATFORMS]


def valid_bound(path: Path | None = None) -> list[dict[str, Any]]:
    return [s for s in list_slots(path) if s["status"] == "valid"]


def get_record(platform: str, path: Path | None = None) -> dict[str, Any] | None:
    plat = normalize_platform(platform)
    data = _load(path)
    for row in data.get("accounts") or []:
        if isinstance(row, dict) and row.get("platform") == plat:
            return row
    return None


def bind_account(
    platform: str,
    secret: str,
    *,
    label: str = "",
    account_id: str = "",
    path: Path | None = None,
) -> dict[str, Any]:
    plat = normalize_platform(platform)
    text = (secret or "").strip()
    if len(text) < MIN_SECRET:
        raise PublishAccountError("publish secret too short")
    status = evaluate_secret(plat, text)
    data = _load(path)
    rows: list[dict[str, Any]] = [
        r
        for r in (data.get("accounts") or [])
        if isinstance(r, dict) and r.get("platform") != plat
    ]
    rec = {
        "platform": plat,
        "label": (label or plat).strip()[:80],
        "account_id": (account_id or plat).strip()[:80],
        "status": status,
        "secret": text,
        "bound_at": _now(),
        "updated_at": _now(),
    }
    prev = get_record(plat, path)
    if prev and prev.get("bound_at"):
        rec["bound_at"] = prev["bound_at"]
    rows.append(rec)
    data["accounts"] = rows
    _save(data, path)
    return public_view(plat, rec)


def unbind_account(platform: str, path: Path | None = None) -> dict[str, Any]:
    plat = normalize_platform(platform)
    data = _load(path)
    data["accounts"] = [
        r
        for r in (data.get("accounts") or [])
        if not (isinstance(r, dict) and r.get("platform") == plat)
    ]
    _save(data, path)
    return public_view(plat, None)
