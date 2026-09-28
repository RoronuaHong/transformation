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
  python cli.py ocr <id> [--force]      画面 OCR(离线 rapidocr,需 SP venv)入检索
  python cli.py ocr --all [--limit N]   批量补齐视频/图片的 OCR 文本
  python cli.py shots <id> [--force]    镜头索引(ffmpeg 场景检测)→ index/shots/<id>.json 片段级 start/end
  python cli.py shots --all [--limit N] 批量补齐全部视频的镜头索引
  python cli.py phash <id> [--force]        dHash 感知哈希(ffmpeg 首帧 9x8 灰度)→ index/phash/<id>.txt
  python cli.py phash --all [--limit N]     批量补齐全部图片/视频的感知哈希
  python cli.py similar <id> [--max-dist N] 画面级近重复检测(汉明距离≤N,按距离升序)
  python cli.py auto [--limit N] [--autotag]  一条命令跑完自动处理链:封面→OCR→镜头索引→pHash→(可选)打标→语义索引(幂等)
  python cli.py agent --task "..."      Deep Agent 多步任务(LLM 拆待办→逐步执行,见 agent.py)
  python cli.py agent --task "..." --write  允许 agent 写(标签/登记;默认只读)
  python cli.py agent --status <id>     查看某次 agent 任务的状态与轨迹
  python cli.py agent --file task.txt   中文任务用 UTF-8 文件传(规避终端 GBK)
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
    ocr_material, ocr_all, _ocr_python,
    build_shot_index, shots_all, shot_index_path,
    phash_material, phash_all, similar_assets, phash_path,
    auto_process_all, pending_processing,
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

    elif cmd == "ocr":
        # 画面 OCR:python cli.py ocr <id> [--force] | ocr --all [--limit N]
        if not _ocr_python():
            print("no OCR python found; hint: 装有 rapidocr 的 venv python,",
                  "或设 VITUAL_OCR_PYTHON=<path>")
            return
        if "--all" in args:
            args.remove("--all")
            limit = 0
            if "--limit" in args:
                i = args.index("--limit")
                limit = int(args[i + 1])
                del args[i:i + 2]
            res = ocr_all(limit=limit, force="--force" in args)
            ok = sum(1 for r in res if r.get("status") == "ok")
            print(f"ocr done: {ok}/{len(res)} ok")
            for r in res:
                print(" ", r)
            return
        mid = next((a for a in args if not a.startswith("-")), "")
        if not mid:
            print("usage: python cli.py ocr <id> [--force] | ocr --all")
            return
        print(ocr_material(mid, force="--force" in args))

    elif cmd == "shots":
        # 镜头索引:python cli.py shots <id> [--force] | shots --all [--limit N]
        if "--all" in args:
            args.remove("--all")
            limit = 0
            if "--limit" in args:
                i = args.index("--limit")
                limit = int(args[i + 1])
                del args[i:i + 2]
            res = shots_all(limit=limit, force="--force" in args)
            ok = sum(1 for r in res if r.get("status") == "ok")
            print(f"shots done: {ok}/{len(res)} ok")
            for r in res:
                print(" ", r)
            return
        mid = next((a for a in args if not a.startswith("-")), "")
        if not mid:
            print("usage: python cli.py shots <id> [--force] | shots --all")
            return
        r = build_shot_index(mid, force="--force" in args)
        print(r)
        sc = shot_index_path(mid)
        if r.get("status") == "ok" and sc:
            print(f"sidecar: {sc}")

    elif cmd == "phash":
        # pHash 近重复检测:python cli.py phash <id> [--force] | phash --all [--limit N]
        if "--all" in args:
            args.remove("--all")
            limit = 0
            if "--limit" in args:
                i = args.index("--limit")
                limit = int(args[i + 1])
                del args[i:i + 2]
            res = phash_all(limit=limit, force="--force" in args)
            ok = sum(1 for r in res if r.get("status") == "ok")
            print(f"phash done: {ok}/{len(res)} ok")
            for r in res:
                print(" ", r)
            return
        mid = next((a for a in args if not a.startswith("-")), "")
        if not mid:
            print("usage: python cli.py phash <id> [--force] | phash --all")
            return
        r = phash_material(mid, force="--force" in args)
        print(r)
        if r.get("status") == "ok" and phash_path(mid):
            print(f"sidecar: {phash_path(mid)}")

    elif cmd == "similar":
        # 近重复列表:python cli.py similar <id> [--max-dist N]
        mid = next((a for a in args if not a.startswith("-")), "")
        if not mid:
            print("usage: python cli.py similar <id> [--max-dist N]")
            return
        md = int(args[args.index("--max-dist") + 1]) if "--max-dist" in args else 10
        r = similar_assets(mid, max_dist=md)
        if r.get("status") == "no_hash":
            print(f"{mid}: 还没有 phash sidecar(先跑 `python cli.py phash {mid}`)")
            return
        print(f"{mid} 的相似素材(汉明距离≤{md}, 共比较 {r.get('total', 0)} 条):")
        for s in r.get("similar", []):
            print(f"  dist={s['dist']:>2}  {s['id']}  {s['name']}")
        if not r.get("similar"):
            print("  (无)")

    elif cmd == "auto":
        # 自动处理链:python cli.py auto [--limit N] [--autotag]
        limit = int(args[args.index("--limit") + 1]) if "--limit" in args else 0
        res = auto_process_all(limit=limit, autotag="--autotag" in args)
        for r in res.get("results", []):
            steps = r.get("steps", {})
            print(f"  {r['id']}  {r.get('ok', 0)}/{r.get('total', len(steps))} 步成功")
            for name, s in steps.items():
                if isinstance(s, dict):
                    extra = s.get("reason") or s.get("path") or s.get("hash") or ""
                    print(f"    {name}: {s.get('status')} {extra}")
                else:
                    print(f"    {name}: {s}")
        print(f"auto done: {res.get('processed', 0)} processed")

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

    elif cmd == "agent":
        import agent
        import json as _json
        if "--status" in args:
            tid = args[args.index("--status") + 1] if len(args) > args.index("--status") + 1 else ""
            print(_json.dumps(agent.agent_status(tid), ensure_ascii=False, indent=2))
            return
        a = args[1:]
        if a and a[0] == "--stdin":           # 中文任务同 search:规避终端 GBK
            try:
                sys.stdin.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
            task = sys.stdin.read().strip()
        elif a and a[0] == "--file" and len(a) > 1:
            with open(a[1], encoding="utf-8-sig") as f:
                task = f.read().strip()
        else:
            task = " ".join(a)
        if not task:
            print(__doc__)
            return
        max_steps = int(args[args.index("--max-steps") + 1]) if "--max-steps" in args else 12
        r = agent.agent_run(task, allow_write="--write" in args, max_steps=max_steps)
        print(_json.dumps(r, ensure_ascii=False, indent=2))

    else:
        print(__doc__)


if __name__ == "__main__":
    main()
