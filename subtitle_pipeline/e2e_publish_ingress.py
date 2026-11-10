"""E2E Stage 5 (publish -> local_stage ledger) + Stage 6 (入库 -> content.db).

Uses TEMP account + TEMP sqlite so we don't pollute the real config/db.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "discover"))

TMP = ROOT / "downloads" / "batch" / "bilibili_E2E_20s" / "_e2e_tmp"
TMP.mkdir(parents=True, exist_ok=True)
ACCT = TMP / "publish_accounts.json"
DB = TMP / "content.db"
os.environ["VITUAL_PUBLISH_ACCOUNTS"] = str(ACCT)

WORK = ROOT / "downloads" / "batch" / "bilibili_E2E_20s" / "media" / "source_subs"

# ---- Stage 5: publish (local_stage ledger) ----
from publish_accounts import bind_account, valid_bound

bind_account(
    "douyin",
    "sessionid=e2e_dummy_12345678",
    label="e2e-test",
    account_id="e2e-dy",
    path=ACCT,
)
print("[e2e-publish] bound accounts:", [s["platform"] for s in valid_bound(ACCT)])

from publish_ops import run_publish

res = run_publish(WORK)
print("[e2e-publish] ok=", res["ok"], "items=", [(i["platform"], i["status"]) for i in res["items"]])
report = (WORK / "media" / "publish" / "report.json").read_text(encoding="utf-8")
print("[e2e-publish] report:", report[:400])

# ---- Stage 6: 入库 (content.db) ----
from discover.content_db import ContentDB
from discover.run_batch import register_work_dir

content = ContentDB(path=DB)
aid = register_work_dir(
    content,
    platform="bilibili",
    video_id="BV1LjWA6aEQ3",
    topic_id="e2e_validate",
    canonical_url="https://www.bilibili.com/video/BV1LjWA6aEQ3",
    work_dir=WORK,
    source_lang="zh",
    title="E2E validation clip",
)
print("[e2e-ingress] article_id=", aid)
row = content._conn.execute(
    "SELECT platform,video_id,title_src,source_lang,status FROM articles WHERE id=?",
    (aid,),
).fetchone()
print("[e2e-ingress] article:", tuple(row))
locs = content._conn.execute(
    "SELECT locale,srt_path,summary_json_path FROM article_locales WHERE article_id=?",
    (aid,),
).fetchall()
print("[e2e-ingress] locales:")
for r in locs:
    print("   ", tuple(r))
