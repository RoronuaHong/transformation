"""Smoke test for the /ask page (nav link + page render)."""
import re
import requests

BASE = "http://127.0.0.1:3010"
out = []

r = requests.get(f"{BASE}/zh/ask", timeout=30)
out.append(f"/zh/ask status: {r.status_code}")
html = r.text
out.append("nav has /zh/ask link: " + str('href="/zh/ask"' in html))
out.append("page title text present: " + str("问问一桌" in html))
m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
out.append("h1: " + (m.group(1).strip() if m else "(none)"))
out.append("ask-panel present: " + str("ask-panel" in html))

with open("smoke_ask_out.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(out))
print("written")
