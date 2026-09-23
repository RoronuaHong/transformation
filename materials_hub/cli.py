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
  python cli.py embed               增量构建语义检索索引(本地 ollama embedding)
  python cli.py embed --force       全量重建
  python cli.py embed --status      只看语义索引状态
"""
import sys
import os
from core import (
    init_hub, HUB, ingest_dir, scan_materials, search,
    all_materials, duplicates, make_thumb, missing_thumbnail_ids,
    thumbs_status, purge_thumbs, get_material,
    build_embeddings, embed_status, embed_probe, expand_query,
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
        a = args[1:]
        if a and a[0] == "--stdin":           # 规避 Windows 终端 GBK 代码页把中文参数搞坏
            try:
                sys.stdin.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
            q = sys.stdin.read().strip()
        elif a and a[0] == "--file" and len(a) > 1:
            with open(a[1], encoding="utf-8-sig") as f:
                q = f.read().strip()
        else:
            q = " ".join(a)
        if not q:
            print("用法: search <关键词> | search --stdin | search --file <UTF-8 文本文件>")
            return
        if "\ufffd" in q or "?" in q:
            print("提示: 关键词疑似被终端编码弄坏;中文查询建议用 "
                  "`search --file q.txt`(UTF-8)或 `search --stdin`。")
        rows = search(q)                      # 默认 auto:有语义索引则混合检索
        for m in rows:
            print(f"{m['id']}  [{m['kind']}]  {m['name']}  tags={m['tags']}")
        ex = expand_query(q)
        if ex != q:
            print(f"-- 同义词扩展: {ex[len(q):].strip()}")
        print(f"-- {len(rows)} match(es)  | 语义索引: {embed_status()['available']}")

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

    elif cmd == "embed":
        st = embed_status()
        if "--status" in args:
            print("embed status:", st)
            return
        if not st["available"]:
            info = embed_probe(refresh=True)
            print("语义检索不可用:未发现本地 embedding 模型。")
            print(f"  探测地址: {info['url']}  原因: {info['err'] or '没有 embedding 能力的模型'}")
            print("  提示: `ollama pull nomic-embed-text`,或用 VITUAL_EMBED_MODEL 指定模型名。")
            return
        t0 = __import__("time").time()
        r = build_embeddings(force="--force" in args,
                             limit=int(args[args.index("--limit") + 1]) if "--limit" in args else 0)
        print("embed done: %s | %.1fs" % (r, __import__("time").time() - t0))
        print("embed status:", embed_status())

    else:
        print(__doc__)


if __name__ == "__main__":
    main()
