#!/usr/bin/env python3
"""Smoke-check Vitual API client + export resources (no MCP stdio)."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from vitual_mcp.client import VitualApiError, VitualClient
from vitual_mcp import resources as res


async def run(*, require_mongo: bool, skip_health: bool) -> int:
    client = VitualClient()
    failures: list[str] = []
    warnings: list[str] = []

    if skip_health:
        print("[smoke] skip /health (--skip-health)")
    else:
        print("[smoke] GET /health")
        try:
            health = await client.health()
            print(json.dumps(health, ensure_ascii=False, indent=2))
            if not health.get("ok"):
                failures.append("health ok!=true")
            if require_mongo and not health.get("mongo"):
                failures.append("mongo required but not connected")
        except VitualApiError as e:
            failures.append(f"health: {e}")
            print(f"FAIL {e}", file=sys.stderr)
            warnings.append("Start API: cd subtitle_pipeline && yarn api")

    print("\n[smoke] export articles index")
    try:
        idx = json.loads(res.articles_index())
        print(f"articles={idx.get('count')} path={idx.get('path')}")
        if not idx.get("count"):
            print("WARN export index empty (run yarn export-site?)")
    except FileNotFoundError as e:
        failures.append(f"export: {e}")
        print(f"FAIL {e}", file=sys.stderr)
    except json.JSONDecodeError as e:
        failures.append(f"export json: {e}")

    print("\n[smoke] MCP module import")
    try:
        from vitual_mcp.server import mcp  # noqa: F401

        print("vitual_mcp.server OK")
    except Exception as e:
        failures.append(f"mcp import: {e}")
        print(f"FAIL {e}", file=sys.stderr)

    if failures:
        print("\n--- FAILED ---", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        for w in warnings:
            print(f"  hint: {w}", file=sys.stderr)
        return 1
    print("\n--- OK ---")
    if warnings:
        for w in warnings:
            print(f"WARN {w}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Vitual MCP/API smoke")
    p.add_argument("--require-mongo", action="store_true", help="Fail if /health mongo=false")
    p.add_argument(
        "--skip-health",
        action="store_true",
        help="Skip /health (export index + MCP import only)",
    )
    args = p.parse_args(argv)
    return asyncio.run(run(require_mongo=args.require_mongo, skip_health=args.skip_health))


if __name__ == "__main__":
    raise SystemExit(main())
