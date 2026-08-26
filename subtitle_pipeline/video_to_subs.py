#!/usr/bin/env python3
"""Backward-compatible entry. Prefer: python pipeline.py run VIDEO --source-lang ja"""

from pipeline import main

if __name__ == "__main__":
    raise SystemExit(main())
