"""Materials Hub — 命令行入口。

用法:
  python cli.py init                初始化目录结构
  python cli.py ingest [path]       整理 path(默认 ingest/) 下文件:去重+分类+命名规范
  python cli.py scan                扫描 materials/ 全树补录索引
  python cli.py search <关键词>     按名称/标签/描述检索
  python cli.py dupes               列出重复文件(按 sha256)
  python cli.py list                列出全部素材
  python cli.py thumbs              批量生成视频封面(ffmpeg 自动发现)
  python cli.py thumbs --purge      清空封面缓存与失败标记
"""
import sys
import os
from core import (
    init_hub, HUB, ingest_dir, scan_materials, search,
    all_materials, duplicates, make_thumb, missing_thumbnail_ids,
    thumbs_status, purge_thumbs, get_material,
)


def main():
    init_hub()
    args = sys.argv[1:]
    cmd = args[0] if args else "help"

    if cmd == "init":
        print("initialized at", HUB)

    elif cmd == "ingest":
        src = args[1] if len(args) > 1 else os.path.join(HUB, "ingest")
        r = ingest_dir(src)
        added = sum(1 for x in r if x and x.get("status") == "added")
        dup = sum(1 for x in r if x and x.get("status") == "duplicate")
        print(f"ingest done: {added} added, {dup} duplicate-skipped")

    elif cmd == "scan":
        r = scan_materials()
        print(f"scan done: {len(r)} new files indexed")

    elif cmd == "search":
        q = " ".join(args[1:])
        rows = search(q)
        for m in rows:
            print(f"{m['id']}  [{m['kind']}]  {m['name']}  tags={m['tags']}")
        print(f"-- {len(rows)} match(es) for '{q}'")

    elif cmd == "dupes":
        for d in duplicates():
            print(f"{d['sha256'][:12]}  x{d['c']}  ids={d['ids']}")

    elif cmd == "list":
        for m in all_materials():
            print(f"{m['id']}  [{m['kind']}]  {m['name']}  {m['size']}B")

    elif cmd == "thumbs":
        if "--purge" in args:
            print("purged:", purge_thumbs())
            return
        st = thumbs_status()
        if not st["ffmpeg"]:
            print("no ffmpeg found; panel falls back to browser-side frame capture.")
            print("hint: set VITUAL_FFMPEG=<path> to point at a binary.")
            return
        ids = missing_thumbnail_ids()
        print(f"ffmpeg: {st['exe']} | need {len(ids)} thumbnail(s)")
        made = fail = 0
        for i, mid in enumerate(ids, 1):
            m = get_material(mid)
            ok = make_thumb(mid)
            made += bool(ok)
            fail += not ok
            print(f"[{i}/{len(ids)}] {'ok  ' if ok else 'FAIL'} {m['name']}", flush=True)
        print(f"thumbs done: {made} ok, {fail} failed | status: {thumbs_status()}")

    else:
        print(__doc__)


if __name__ == "__main__":
    main()
