"""
52pojie.cn 全站遍历：筛选「职场类 + 绿色工具」帖子/页面

用法：
  1. pip install playwright lxml
  2. 首次运行：把 HEADLESS=False，浏览器弹出后手动登录 52pojie
  3. 登录成功后改 HEADLESS=True 再跑，结果写入 results.jsonl
"""

import json
import os
import random
import re
import ssl
import time
import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright

SITE = "https://www.52pojie.cn/"
SITEMAP_URL = urljoin(SITE, "sitemap.xml")

# 本机 Edge（若无则设为 None，需 playwright install chromium）
EDGE_EXECUTABLE_PATH = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

STORAGE_STATE_PATH = os.path.abspath("./storage_state_52pojie.json")
# 第一次建议设为 True：在弹出的 Edge 里手动登录，然后脚本导出 storage_state
CAPTURE_LOGIN = True
# 让你在浏览器中完成登录的等待时间（秒）
CAPTURE_WAIT_SECONDS = 60

WORK_KEYWORDS = [
    "职场", "求职", "简历", "面试", "招聘", "办公", "会议", "效率", "职业", "晋升",
]
GREEN_KEYWORDS = [
    "绿色", "绿色软件", "绿色版", "免安装", "绿色工具", "便携", "绿色下载",
]

BLOCK_SIGNS = ["[Register]", "注册", "Please enable JavaScript", "wzwschallenge"]

RESULTS_PATH = "results.jsonl"
MAX_PAGES = 40  # 更快验证
# 登录捕获阶段建议 headful
HEADLESS = False if CAPTURE_LOGIN else True
WAIT_MS = 6000

ssl_ctx = ssl.create_default_context()


def fetch_text(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout, context=ssl_ctx) as resp:
        return resp.read()


def get_sitemap_urls() -> list[str]:
    data = fetch_text(SITEMAP_URL, timeout=60)
    root = ET.fromstring(data)
    urls: list[str] = []
    for loc in root.iter():
        if loc.tag.lower().endswith("loc") and loc.text:
            urls.append(loc.text.strip())
    return list(dict.fromkeys(urls))


def normalize_text(text: str) -> str:
    text = text.replace("\u00a0", " ")
    return re.sub(r"\s+", " ", text)


def is_blocked(text: str) -> bool:
    lower = text.lower()
    return any(sign.lower() in lower for sign in BLOCK_SIGNS)


def match_keywords(text: str) -> tuple[bool, bool, bool]:
    work_hit = any(k in text for k in WORK_KEYWORDS)
    green_hit = any(k in text for k in GREEN_KEYWORDS)
    return work_hit and green_hit, work_hit, green_hit


def run() -> None:
    urls = get_sitemap_urls()[:MAX_PAGES]
    print(f"[+] sitemap urls: {len(urls)}")

    if os.path.exists(RESULTS_PATH):
        os.remove(RESULTS_PATH)

    # 1) 若没有 storage_state，就先 headful 打开浏览器让你登录
    need_capture = CAPTURE_LOGIN or (not os.path.exists(STORAGE_STATE_PATH))
    launch_args: dict = {"headless": HEADLESS}
    if EDGE_EXECUTABLE_PATH and os.path.exists(EDGE_EXECUTABLE_PATH):
        launch_args["executable_path"] = EDGE_EXECUTABLE_PATH

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**launch_args)

        if need_capture:
            print("[*] 正在打开浏览器用于登录态捕获...")
            context = browser.new_context()
            page = context.new_page()
            page.goto(SITE, timeout=60000, wait_until="domcontentloaded")
            print("[*] 请在弹出的浏览器中完成登录。完成后请回到此窗口按回车继续。")
            print(f"[*] 脚本将等待 {CAPTURE_WAIT_SECONDS}s 后自动导出登录态（不需要你在控制台输入）。")
            time.sleep(CAPTURE_WAIT_SECONDS)
            context.storage_state(path=STORAGE_STATE_PATH)
            print(f"[+] 已导出登录态: {STORAGE_STATE_PATH}")
            context.close()
            context = browser.new_context(storage_state=STORAGE_STATE_PATH)
        else:
            context = browser.new_context(storage_state=STORAGE_STATE_PATH)

        page = context.new_page()

        # 登录态校验：如果仍然出现注册/引导页，说明 cookies 并未真正生效
        verify_url = next((u for u in urls if "thread-" in u), urls[0] if urls else SITE)
        try:
            page.goto(verify_url, timeout=60000, wait_until="domcontentloaded")
            page.wait_for_timeout(5000)
            verify_text = normalize_text((page.inner_text("body") or ""))
            if is_blocked(verify_text):
                print("[!] storage_state 验证失败：仍出现登录/注册引导页。请重新运行（CAPTURE_LOGIN=True），并确保登录完成后再等脚本自动导出。")
                context.close()
                browser.close()
                return
        except Exception as exc:
            print(f"[!] storage_state 验证异常：{exc!r}（将继续尝试抓取）")

        matched = 0
        for index, url in enumerate(urls, start=1):
            try:
                print(f"[{index}/{len(urls)}] {url}")
                page.goto(url, timeout=60000, wait_until="domcontentloaded")
                page.wait_for_timeout(WAIT_MS)

                title = page.title() or ""
                body = page.inner_text("body") or ""
                text = normalize_text(f"{title} {body}")

                if is_blocked(text):
                    print("    -> blocked/guide page, skip")
                    continue

                ok, work_hit, green_hit = match_keywords(text)
                if ok:
                    matched += 1
                    record = {
                        "url": url,
                        "title": title,
                        "work_hit": work_hit,
                        "green_hit": green_hit,
                        "snippet": text[:300],
                    }
                    with open(RESULTS_PATH, "a", encoding="utf-8") as handle:
                        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                    print(f"    -> MATCH! total={matched}")
                else:
                    print("    -> no match")

                time.sleep(random.uniform(0.2, 0.8))
            except Exception as exc:
                print(f"    -> error: {exc!r}")

        context.close()
        browser.close()

    print(f"[+] done. matched={matched}, output={RESULTS_PATH}")


if __name__ == "__main__":
    print("[!] 第一次请在弹出的 Edge 里完成登录，然后回到此窗口按回车；之后会写出 storage_state。")
    run()
