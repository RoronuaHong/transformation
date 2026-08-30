"""HTTP client for Vitual FastAPI backend."""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

DEFAULT_BASE = "http://127.0.0.1:8800"
DEFAULT_TOKEN = "local-admin"
TERMINAL = frozenset({"done", "failed", "dead", "cancelled"})


class VitualApiError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class VitualClient:
    def __init__(
        self,
        base_url: str | None = None,
        admin_token: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.base_url = (base_url or os.environ.get("VITUAL_API_URL") or DEFAULT_BASE).rstrip("/")
        self.admin_token = admin_token or os.environ.get("VITUAL_ADMIN_TOKEN") or DEFAULT_TOKEN
        self.timeout = timeout

    def _admin_headers(self) -> dict[str, str]:
        return {"X-Admin-Token": self.admin_token}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        admin: bool = False,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        form: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        headers = self._admin_headers() if admin else {}
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        url = f"{self.base_url}{path}"
        req_timeout = timeout if timeout is not None else self.timeout
        try:
            async with httpx.AsyncClient(timeout=req_timeout, trust_env=False) as client:
                resp = await client.request(
                    method,
                    url,
                    headers=headers,
                    json=json_body,
                    params=params,
                    files=files,
                    data=form,
                )
        except httpx.ConnectError as e:
            raise VitualApiError(
                f"Cannot reach Vitual API at {self.base_url}. Start it: cd subtitle_pipeline && yarn api",
            ) from e
        except httpx.TimeoutException as e:
            raise VitualApiError(f"Request timed out: {method} {path}") from e

        if resp.status_code >= 400:
            detail: Any
            try:
                detail = resp.json()
            except json.JSONDecodeError:
                detail = resp.text
            raise VitualApiError(
                f"HTTP {resp.status_code} on {path}: {detail}",
                status=resp.status_code,
                body=detail,
            )
        if not resp.content:
            return {}
        try:
            return resp.json()
        except json.JSONDecodeError:
            return {"raw": resp.text}

    async def health(self) -> dict[str, Any]:
        return await self._request("GET", "/health")

    async def admin_status(self) -> dict[str, Any]:
        return await self._request("GET", "/admin/status", admin=True)

    async def try_probe(self, urls: list[str], sessionid: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"urls": urls}
        if sessionid:
            body["sessionid"] = sessionid
        return await self._request("POST", "/api/try/probe", json_body=body)

    async def try_duration(self, url: str) -> dict[str, Any]:
        return await self._request("GET", "/api/try/duration", params={"url": url})

    async def try_submit_urls(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/api/try/urls", json_body=body)

    async def try_poll(self, job_id: int) -> dict[str, Any]:
        return await self._request("GET", f"/api/try/{job_id}")

    async def try_wait(
        self,
        job_id: int,
        *,
        poll_interval_sec: float = 5.0,
        timeout_sec: float = 3600.0,
    ) -> dict[str, Any]:
        import asyncio
        import time

        deadline = time.monotonic() + timeout_sec
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last = await self.try_poll(job_id)
            status = str(last.get("status") or "").lower()
            if status in TERMINAL:
                return last
            await asyncio.sleep(max(1.0, poll_interval_sec))
        raise VitualApiError(
            f"Job {job_id} did not finish within {timeout_sec}s. Last status: {last.get('status')!r}",
            body=last,
        )

    async def try_wait_all(
        self,
        job_ids: list[int],
        *,
        poll_interval_sec: float = 5.0,
        timeout_sec: float = 3600.0,
    ) -> dict[str, Any]:
        import asyncio

        unique = []
        seen: set[int] = set()
        for jid in job_ids:
            i = int(jid)
            if i not in seen:
                seen.add(i)
                unique.append(i)
        if not unique:
            raise VitualApiError("job_ids must not be empty")

        async def one(jid: int) -> dict[str, Any]:
            try:
                snap = await self.try_wait(
                    jid,
                    poll_interval_sec=poll_interval_sec,
                    timeout_sec=timeout_sec,
                )
                return {"job_id": jid, "ok": True, **snap}
            except VitualApiError as e:
                body = e.body if isinstance(e.body, dict) else {}
                return {
                    "job_id": jid,
                    "ok": False,
                    "status": body.get("status") if body else "failed",
                    "error": str(e),
                    "body": e.body,
                }

        results = await asyncio.gather(*(one(jid) for jid in unique))
        failed = [r for r in results if not r.get("ok") or str(r.get("status") or "").lower() == "failed"]
        return {
            "count": len(results),
            "failed": len(failed),
            "all_done": len(failed) == 0,
            "jobs": results,
        }

    async def admin_inbox(
        self, url: str, *, topic: str = "inbox", title: str | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"url": url, "topic": topic}
        if title:
            body["title"] = title
        return await self._request("POST", "/admin/inbox", admin=True, json_body=body)

    async def admin_discover(
        self,
        *,
        topic: str | None = None,
        dry_run: bool = False,
        mock: bool = False,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"dry_run": dry_run, "mock": mock}
        if topic:
            body["topic"] = topic
        return await self._request("POST", "/admin/discover", admin=True, json_body=body)

    async def admin_batch(
        self,
        *,
        fast: bool = True,
        limit: int = 1,
        requeue_failed: bool = True,
    ) -> dict[str, Any]:
        body = {"fast": fast, "limit": limit, "requeue_failed": requeue_failed}
        return await self._request("POST", "/admin/batch", admin=True, json_body=body)

    async def admin_export(self) -> dict[str, Any]:
        return await self._request("POST", "/admin/export", admin=True, json_body={})

    async def admin_links(self, *, limit: int = 100) -> dict[str, Any]:
        return await self._request("GET", "/admin/links", admin=True, params={"limit": limit})

    async def admin_logs(self, *, limit: int = 200) -> dict[str, Any]:
        return await self._request("GET", "/admin/logs", admin=True, params={"limit": limit})

    async def admin_alerts(self, *, unacked: bool = False) -> dict[str, Any]:
        return await self._request(
            "GET", "/admin/alerts", admin=True, params={"unacked": str(unacked).lower()}
        )

    async def admin_ack_alert(self, alert_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/admin/alerts/{alert_id}/ack", admin=True)

    async def admin_runs(self, *, limit: int = 50) -> dict[str, Any]:
        return await self._request("GET", "/admin/runs", admin=True, params={"limit": limit})

    async def try_submit_upload(
        self,
        file_path: str,
        *,
        topic: str = "general",
        frames: str = "auto",
        gif_sec: float = 4.0,
        clips: list[dict[str, float]] | None = None,
        gif_ranges: list[dict[str, float]] | None = None,
        langs: str | list[str] | None = None,
        timeout: float = 600.0,
    ) -> dict[str, Any]:
        from pathlib import Path

        path = Path(file_path).expanduser().resolve()
        if not path.is_file():
            raise VitualApiError(f"Upload file not found: {path}")
        data = path.read_bytes()
        name = path.name
        langs_field = ""
        if langs is not None:
            langs_field = json.dumps(langs) if isinstance(langs, list) else str(langs)
        form: dict[str, str] = {
            "topic": topic,
            "frames": frames,
            "gif_sec": str(gif_sec),
            "clips": json.dumps(clips or []),
            "gif_ranges": json.dumps(gif_ranges or []),
            "langs": langs_field,
        }
        files = {"file": (name, data, "application/octet-stream")}
        return await self._request(
            "POST",
            "/api/try/upload",
            files=files,
            form=form,
            timeout=timeout,
        )

    async def try_submit_uploads(
        self,
        file_paths: list[str],
        *,
        topic: str = "general",
        frames: str = "auto",
        gif_sec: float = 4.0,
        clips: list[dict[str, float]] | None = None,
        gif_ranges: list[dict[str, float]] | None = None,
        langs: str | list[str] | None = None,
        want_translate: bool = True,
        want_notes: bool = True,
        timeout: float = 900.0,
    ) -> dict[str, Any]:
        from pathlib import Path

        if not file_paths:
            raise VitualApiError("file_paths must not be empty")
        multipart_files: list[tuple[str, tuple[str, bytes, str]]] = []
        entries: list[dict[str, Any]] = []
        for raw in file_paths:
            path = Path(raw).expanduser().resolve()
            if not path.is_file():
                raise VitualApiError(f"Upload file not found: {path}")
            name = path.name
            multipart_files.append(("files", (name, path.read_bytes(), "application/octet-stream")))
            entries.append({"filename": name})
        langs_field = ""
        if langs is not None:
            langs_field = json.dumps(langs) if isinstance(langs, list) else str(langs)
        form: dict[str, str] = {
            "entries": json.dumps(entries),
            "topic": topic,
            "frames": frames,
            "gif_sec": str(gif_sec),
            "clips": json.dumps(clips or []),
            "gif_ranges": json.dumps(gif_ranges or []),
            "langs": langs_field,
            "want_translate": "true" if want_translate else "false",
            "want_notes": "true" if want_notes else "false",
        }
        return await self._request(
            "POST",
            "/api/try/uploads",
            files=multipart_files,
            form=form,
            timeout=timeout,
        )
