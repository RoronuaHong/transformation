"""Full-stack verification: Docker, Milvus, KB service, site page, AnythingLLM, Git."""
import json
import os
import subprocess
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(BASE)
KB_API = "http://127.0.0.1:8910"
SITE = os.environ.get("SITE_URL", "http://127.0.0.1:3011")
ANYTHINGLLM = "http://127.0.0.1:3005"

lines = []


def log(label: str, ok: bool, detail: str = "") -> None:
    lines.append(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))


def http_get(url: str, timeout: int = 20):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "ignore")


def post_json(url: str, payload: dict, timeout: int = 300):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8", "ignore"))


# 1. Docker containers
try:
    out = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}|{{.Status}}"],
        capture_output=True, text=True, timeout=60,
        cwd=BASE,
    ).stdout
    deskline = [l for l in out.splitlines() if "deskline" in l]
    ups = [l for l in deskline if "Up" in l]
    log("Docker 容器", len(ups) == 4, f"{len(ups)}/4 up: " + ", ".join(
        l.split("|")[0].replace("deskline-", "") for l in ups))
except Exception as e:
    log("Docker 容器", False, str(e))

# 2. KB health
try:
    st, body = http_get(f"{KB_API}/health")
    log("知识库 /health", st == 200, body)
except Exception as e:
    log("知识库 /health", False, str(e))

# 3. KB search (Milvus retrieval)
try:
    st, data = post_json(f"{KB_API}/search", {"query": "周报怎么写", "top_k": 3}, timeout=120)
    results = data.get("results", [])
    log("知识库 /search (Milvus 检索)", st == 200 and len(results) > 0,
        f"{len(results)} 条，最高分 {max(r['score'] for r in results):.3f}" if results else "无结果")
except Exception as e:
    log("知识库 /search", False, str(e))

# 4. KB ask (retrieval + local LLM)
try:
    st, data = post_json(
        f"{KB_API}/ask", {"query": "一桌 Deskline 有哪些职场资产包？分别多少钱？", "top_k": 5}
    )
    ans = data.get("answer", "")
    srcs = data.get("sources", [])
    ok = st == 200 and "19.9" in ans and len(srcs) > 0
    log("知识库 /ask (检索+生成)", ok, f"答案 {len(ans)} 字，引用 {len(srcs)} 条")
except Exception as e:
    log("知识库 /ask", False, str(e))

# 5. Site /ask page
try:
    st, html = http_get(f"{SITE}/zh/ask")
    ok = st == 200 and 'href="/zh/ask"' in html and "问问一桌" in html and "ask-panel" in html
    log("站点 /zh/ask 页面", ok, f"status {st}")
except Exception as e:
    log("站点 /zh/ask 页面", False, str(e))

# 6. AnythingLLM
try:
    st, _ = http_get(ANYTHINGLLM)
    log("AnythingLLM (B 方案)", st == 200, f"status {st}")
except Exception as e:
    log("AnythingLLM", False, str(e))

# 7. Git sync with origin
try:
    subprocess.run(["git", "fetch", "origin"], cwd=REPO, capture_output=True, timeout=120)
    branch = subprocess.run(
        ["git", "status", "-sb"], capture_output=True, text=True, timeout=60, cwd=REPO
    ).stdout.splitlines()
    first = branch[0] if branch else ""
    ahead = "ahead" in first
    behind = "behind" in first
    porcelain = subprocess.run(
        ["git", "status", "--porcelain"], capture_output=True, text=True, timeout=60, cwd=REPO
    ).stdout
    dirty = len([l for l in porcelain.splitlines() if l.strip()])
    log("Git 与 origin 同步", not ahead and not behind,
        f"{first.strip()}，工作区未提交 {dirty} 项")
except Exception as e:
    log("Git 同步", False, str(e))

with open(os.path.join(BASE, "verify_out.txt"), "w", encoding="utf-8") as f:
    f.write("\n".join(lines) + "\n")
print("written")
