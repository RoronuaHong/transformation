"""Admin token gate + optional webhook alerts."""

from __future__ import annotations

import json
import urllib.request
from typing import Annotated

from fastapi import Header, HTTPException

from api.mongo import MongoStore
from api.settings import Settings, get_settings


def require_admin(
    x_admin_token: Annotated[str | None, Header()] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Require X-Admin-Token or Bearer token matching VITUAL_ADMIN_TOKEN."""
    expected = (get_settings().admin_token or "").strip()
    if not expected:
        raise HTTPException(status_code=500, detail="admin token not configured")
    got = (x_admin_token or "").strip()
    if not got and authorization and authorization.lower().startswith("bearer "):
        got = authorization[7:].strip()
    if got != expected:
        raise HTTPException(status_code=401, detail="unauthorized")


def post_webhook(url: str, payload: dict) -> None:
    if not url:
        return
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=8)
    except Exception:
        return


def emit_alert(
    store: MongoStore,
    settings: Settings,
    *,
    title: str,
    message: str,
    severity: str = "error",
    run_id: str | None = None,
    extra: dict | None = None,
) -> str:
    alert_id = store.raise_alert(
        title, message, severity=severity, run_id=run_id, extra=extra
    )
    store.write_log(
        "error" if severity == "error" else "warn",
        "alert",
        f"{title}: {message}",
        run_id=run_id,
        extra={"alert_id": alert_id, **(extra or {})},
    )
    post_webhook(
        settings.alert_webhook_url,
        {
            "alert_id": alert_id,
            "title": title,
            "message": message,
            "severity": severity,
            "run_id": run_id,
        },
    )
    return alert_id
