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
  python cli.py autotag             用本地 LLM 为素材自动补描述/标签(需 ollama chat 模型)
  python cli.py autotag --limit 10  只处理前 10 条
  python cli.py autotag --dry       只输出模型建议,不写库
  python cli.py autotag --rule      离线规则打标:确定性关键词提取标签(预览,不写库)
  python cli.py autotag --rule --apply  写库(仅追加标签、为空时补中文描述,非破坏式)
  python cli.py autotag --rule --undo   回滚规则打标写入的标签(保留 bridge 的 bilibili)
"""
import sys
import os
from core import (
    init_hub, HUB, ingest_dir, scan_materials, search,
    all_materials, duplicates, make_thumb, missing_thumbnail_ids,
    thumbs_status, purge_thumbs, get_material,
    build_embeddings, embed_status, embed_probe, expand_query,
    auto_tag_all, chat_models, rule_tag_all, rule_tag_cleanup,
    autotag_undo, AUTOTAG_BACKUP,
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

    elif cmd == "autotag":
        if "--rule" in args:
            if "--undo" in args:
                n = rule_tag_cleanup()
                print(f"rule-tag UNDONE: removed {n} rule-added tags "
                      f"(bridge 的 bilibili 等保留)")
                return
            dry = "--apply" not in args
            limit = int(args[args.index("--limit") + 1]) if "--limit" in args else 0
            res = rule_tag_all(limit=limit, dry=dry)
            changed = [x for x in res if (x.get("added_tags") or (dry and x.get("added")))]
            print(f"rule-tag {'PREVIEW(dry)' if dry else 'APPLIED'}: "
                  f"{len(res)} scanned, {len(changed)} with new tags")
            for x in changed[:25]:
                print(f"  {x['id']}  +{(x.get('added_tags') or x.get('added'))}  | {x.get('desc')}")
            if dry:
                print("  (加 --apply 写库;如已应用可用 --undo 回滚)")
            return
        if not chat_models():
            print("自动打标(LLM)不可用:未发现本地 chat 模型。")
            print("  提示: `ollama pull <一个 chat 模型, 如 qwen2.5:7b>`;或先用离线规则打标:")
            print("        python cli.py autotag --rule        # 预览")
            print("        python cli.py autotag --rule --apply # 写库(仅追加标签/补空描述)")
            return
        if "--undo" in args:
            n = autotag_undo()
            print(f"autotag UNDONE: 已恢复 {n} 条素材的 tags"
                  f"{' (备份 ' + os.path.basename(AUTOTAG_BACKUP) + ' 已删除)' if n else ''}")
            return
        dry = "--dry" in args
        limit = int(args[args.index("--limit") + 1]) if "--limit" in args else 0
        res = auto_tag_all(limit=limit, dry=dry)
        ok = sum(1 for x in res if x["status"] in ("ok", "dry"))
        skip = sum(1 for x in res if x["status"] == "skipped")
        err = sum(1 for x in res if x["status"] in ("error", "parse_error"))
        print(f"autotag done: {ok} processed, {skip} skipped, {err} failed"
              f"{' (dry-run, 未写库)' if dry else ''}")
        for x in res[:25]:
            extra = x.get("desc") or x.get("reason") or ""
            print(f"  {x['id']}  {x['status']}  {extra}")

    else:
        print(__doc__)


if __name__ == "__main__":
    main()
