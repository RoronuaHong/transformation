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
  python cli.py near-dupes [--max-dist N] [--limit N]  全库近重复报告(对+簇,一实体多引用)
  python cli.py imgsearch <id|path> [--max-dist N] [--limit N] [--mode phash|clip|auto]
  python cli.py imgsearch --text "厨房"   以文搜图(需 CLIP 索引)
  python cli.py imgembed [--force] [--limit N] [--status]  建/查 CLIP 图像向量索引
  python cli.py auto [--limit N] [--autotag]  一条命令跑完自动处理链:封面→OCR→镜头索引→pHash→(可选)打标→语义索引(幂等)
  python cli.py facets [--limit N] [--link-clips|--link-parents]  补 DAM 面标签 + 回填 parent:
  python cli.py facets [--scrub]  去掉与 parent: 重复的旧 from: 别名(库内治理)
  python cli.py govern [--limit N]       全库治理/合规扫描(占位/缺描述/未分类/无标签)
  python cli.py readiness --job <id>    某 job 分发渠道就绪度(封面/镜头/clip 父链/标签齐备)
  python cli.py feedback --action X [--reject] [--note Y]   记录 Agent 动作采纳/否决(学习闭环)
  python cli.py learning [--limit N]     汇总反馈学习日志(各动作采纳率)
  python cli.py agent --task "..."      Deep Agent 多步任务(LLM 拆待办→逐步执行,见 agent.py)
  python cli.py agent --task "..." --write  允许 agent 写(标签/登记;默认只读)
  python cli.py agent --status <id>     查看某次 agent 任务的状态与轨迹
  python cli.py agent --cancel <id>     协作式取消进行中的任务(步边界终止,进度保留)
  python cli.py agent --resume <id> [--extra-steps 6]  续跑步数耗尽(max_steps_reached)的任务
  python cli.py agent --list [--limit N]  盘点历史任务(状态/步数/创建时间)
  python cli.py agent --cleanup [--max-age 72]  清理超龄的终态任务工作区(running 永不删)
  python cli.py agent --file task.txt   中文任务用 UTF-8 文件传(规避终端 GBK)
"""
import sys
import os
import json
from core import (
    init_hub, HUB, ingest_dir, scan_materials, search,
    all_materials, duplicates, make_thumb, missing_thumbnail_ids,
    thumbs_status, purge_thumbs, get_material,
    build_embeddings, embed_status, embed_probe, expand_query,
    auto_tag_all, chat_models, rule_tag_all, rule_tag_cleanup,
    autotag_undo, AUTOTAG_BACKUP,
    ocr_material, ocr_all, _ocr_python,
    visual_material, visual_all,
    build_shot_index, shots_all, shot_index_path,
    phash_material, phash_all, similar_assets, phash_path, near_duplicate_report,
    search_by_image, search_by_text_image, clip_probe,
    build_image_embeddings, image_embed_status, build_clip_frame_embeddings,
    auto_process_all, pending_processing,
    apply_media_facet_tags, link_relation_parents, scrub_deprecated_from_tags,
)
from core import (
    governance_report, distribution_readiness, log_feedback, learning_summary, add_veto,
    normalize_name, normalize_all_names,
    tech_material, tech_all, tech_stats, read_tech,
    describe_material, describe_all,
    write_run_record, read_run_record,
    write_understand_record, ops_summary,
    backfill_lang_tags,
    job_lang_readiness,
    SITE_LANGS, normalize_lang_tag, VISUAL_KINDS,
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

    elif cmd == "visual":
        # 画面描述:python cli.py visual <id> [--force] | visual --all [--limit N]
        rest = args[1:]                       # 去掉命令 token,避免把 "visual" 当成 id
        if "--all" in rest:
            rest.remove("--all")
            limit = 0
            if "--limit" in rest:
                i = rest.index("--limit")
                limit = int(rest[i + 1])
                del rest[i:i + 2]
            res = visual_all(limit=limit, force="--force" in rest)
            ok = sum(1 for r in res if r.get("status") == "ok")
            print(f"visual done: {ok}/{len(res)} ok")
            for r in res:
                print(" ", r)
            return
        mid = next((a for a in rest if not a.startswith("-")), "")
        if not mid:
            print("usage: python cli.py visual <id> [--force] | visual --all")
            return
        print(visual_material(mid, force="--force" in rest))

    elif cmd == "understand":
        # 理解记录:python cli.py understand <id> [--force] | understand --all
        rest = args[1:]
        if "--all" in rest:
            n = 0
            for m in all_materials():
                write_understand_record(m["id"])
                n += 1
            print(f"understand done: wrote {n} records")
            return
        mid = next((a for a in rest if not a.startswith("-")), "")
        if not mid:
            print("usage: python cli.py understand <id> [--force] | understand --all")
            return
        print(json.dumps(write_understand_record(mid), ensure_ascii=False, indent=2))

    elif cmd == "run":
        # 入库链路状态:python cli.py run <id> [--force] | run --all
        rest = args[1:]
        if "--all" in rest:
            n = 0
            for m in all_materials():
                if m.get("kind") in VISUAL_KINDS or m.get("kind") == "audio":
                    write_run_record(m["id"])
                    n += 1
            print(f"run done: wrote {n} records")
            return
        mid = next((a for a in rest if not a.startswith("-")), "")
        if not mid:
            print("usage: python cli.py run <id> [--force] | run --all")
            return
        rec = read_run_record(mid)
        print(json.dumps(rec, ensure_ascii=False, indent=2) if rec else "no run record")

    elif cmd == "ops":
        # 运营汇总(按语种拆开计数):python cli.py ops
        print(json.dumps(ops_summary(), ensure_ascii=False, indent=2))

    elif cmd == "langs":
        # 受控语种词表:python cli.py langs
        print("SITE_LANGS:", ", ".join(SITE_LANGS))
        print("normalize_lang_tag('zh-hant') ->", normalize_lang_tag("zh-hant"))
        print("normalize_lang_tag('Japanese') ->", normalize_lang_tag("Japanese"))
        print("normalize_lang_tag('chinese') ->", normalize_lang_tag("chinese"))

    elif cmd == "langtags":
        # 回填 lang: 标签(历史字幕缺标签):python cli.py langtags --apply
        rest = args[1:]
        if "--apply" in rest:
            print(json.dumps(backfill_lang_tags(), ensure_ascii=False))
        else:
            print("dry-run: 加 --apply 执行回填(只增 lang:,不动其它标签)")

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

    elif cmd in ("near-dupes", "neardupes", "near_dupes"):
        # 全库近重复报告:python cli.py near-dupes [--max-dist N] [--limit N]
        md = int(args[args.index("--max-dist") + 1]) if "--max-dist" in args else 10
        lim = int(args[args.index("--limit") + 1]) if "--limit" in args else 50
        r = near_duplicate_report(max_dist=md, limit_pairs=lim)
        print(f"near-dupes: hashed={r['hashed']} pairs={r['pair_count']} "
              f"clusters={r['cluster_count']} (max_dist≤{md})")
        for c in r.get("clusters") or []:
            print(f"  cluster size={c['size']}: {', '.join(c['ids'])}")
            for nm in c.get("names") or []:
                if nm:
                    print(f"           · {nm}")
        if not r.get("clusters"):
            print("  (无近重复簇)")
        shown = r.get("pairs") or []
        if shown and r["pair_count"] > len(shown):
            print(f"  (pairs 仅展示前 {len(shown)}/{r['pair_count']})")

    elif cmd in ("imgsearch", "img-search", "search-image"):
        # 以图/以文搜图
        lim = int(args[args.index("--limit") + 1]) if "--limit" in args else 20
        if "--text" in args:
            i = args.index("--text")
            text = args[i + 1] if i + 1 < len(args) else ""
            if not text or text.startswith("-"):
                # 允许多词:取 --text 后到下一 -- 前
                parts = []
                for a in args[i + 1:]:
                    if a.startswith("-") and a not in ("--"):
                        break
                    parts.append(a)
                text = " ".join(parts).strip()
            if not text:
                print("usage: python cli.py imgsearch --text \"厨房场景\"")
                return
            r = search_by_text_image(text, limit=lim)
            print(f"imgsearch-text status={r.get('status')} mode={r.get('mode')} "
                  f"model={r.get('model', '')}")
            if r.get("hint"):
                print(" ", r["hint"])
            for s in r.get("matches") or []:
                print(f"  score={s.get('score')}  {s['id']}  {s.get('kind','')}  {s.get('name','')}")
            return
        positional = [a for a in args[1:] if not a.startswith("-")]
        q = positional[0] if positional else ""
        if not q:
            print("usage: python cli.py imgsearch <id|path> [--mode phash|clip|auto]")
            print("       python cli.py imgsearch --text \"query\"")
            return
        md = int(args[args.index("--max-dist") + 1]) if "--max-dist" in args else 10
        mode = args[args.index("--mode") + 1] if "--mode" in args else "auto"
        r = search_by_image(q, max_dist=md, limit=lim, mode=mode)
        print(f"imgsearch mode={r.get('mode')} status={r.get('status')} "
              f"total={r.get('total', 0)} clip={clip_probe().get('ok')}")
        if r.get("hint"):
            print(" ", r["hint"])
        for s in r.get("matches") or []:
            if "dist" in s:
                print(f"  dist={s['dist']:>2}  {s['id']}  {s.get('kind','')}  {s.get('name','')}")
            else:
                print(f"  score={s.get('score')}  {s['id']}  {s.get('kind','')}  {s.get('name','')}")
        if r.get("status") == "ok" and not r.get("matches"):
            print("  (无匹配;可先 python cli.py phash --all 或 imgembed)")

    elif cmd in ("imgembed", "clip-embed", "image-embed"):
        if "--status" in args:
            print("imgembed status:", image_embed_status())
            print("clip probe:", clip_probe(refresh=True))
            return
        limit = int(args[args.index("--limit") + 1]) if "--limit" in args else 0
        if "--frames" in args:
            # 通道 B:视频多帧 CLIP 索引(以文搜帧);frames 数可 --frames N 覆盖默认 5
            fr = 5
            if "--fr" in args:
                fr = int(args[args.index("--fr") + 1])
            r = build_clip_frame_embeddings(frames=fr, force="--force" in args, limit=limit)
            print("clip-frame-embed done:", r)
            return
        r = build_image_embeddings(force="--force" in args, limit=limit)
        print("imgembed done:", r)
        print("imgembed status:", image_embed_status())

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

    elif cmd == "facets":
        limit = int(args[args.index("--limit") + 1]) if "--limit" in args else 0
        r = apply_media_facet_tags(limit=limit)
        print(f"facets: scanned={r.get('scanned')} patched={r.get('patched')}")
        if "--link-clips" in args or "--link-parents" in args:
            link = link_relation_parents(limit=limit)
            print(f"link-parents: linked={link.get('linked')} "
                  f"clips={link.get('clips')} components={link.get('components')}")
        if "--scrub" in args:
            s = scrub_deprecated_from_tags(limit=limit)
            print(f"scrub-from: scanned={s.get('scanned')} cleaned={s.get('cleaned')}")

    elif cmd == "govern":
        import json as _j
        r = governance_report(limit=int(args[args.index("--limit") + 1]) if "--limit" in args else 50)
        print(_j.dumps(r, ensure_ascii=False, indent=2))

    elif cmd == "rename":
        # 命名规范化(见 naming.py 的六条硬规则):默认 dry-run,加 --apply 才写库。
        # 只改索引显示名 name;orig_name/rel_path/external_path/磁盘文件一律不动。
        import naming as _nm
        dry = "--apply" not in args
        n = int(args[args.index("--samples") + 1]) if "--samples" in args else 10
        ms = all_materials()
        plan = _nm.plan_renames(ms)
        for m, new in plan[:n]:
            print(f"  {m.get('name')}  ->  {new}")
        changed, total = normalize_all_names(dry_run=dry)
        print(f"rename: {'DRY-RUN(未写入)' if dry else 'APPLIED'} "
              f"changed={changed}/{total} unique={len({x for _, x in plan})}/{total}")

    elif cmd == "tech":
        # 技术元数据(Cloudinary MAM / Adobe AEM「技术类元数据」):时长/分辨率/编码/帧率/采样率。
        # 派生数据 → 只落 sidecar index/tech/<id>.json,可重建;不写 description(会稀释排序)。
        import json as _j
        if "--stats" in args:
            print(_j.dumps(tech_stats(), ensure_ascii=False, indent=2))
        elif "--all" in args:
            lim = int(args[args.index("--limit") + 1]) if "--limit" in args else 0
            print(_j.dumps(tech_all(limit=lim, force="--force" in args), ensure_ascii=False))
        else:
            mid = args[1] if len(args) > 1 and not args[1].startswith("--") else ""
            if not mid:
                print("usage: cli.py tech <id> [--force] | tech --all [--limit N] [--force] | tech --stats")
            else:
                print(_j.dumps(tech_material(mid, force="--force" in args),
                               ensure_ascii=False, indent=2))

    elif cmd == "describe":
        # 描述性元数据(Adobe AEM「摄入即应用描述性元数据」):确定性生成,
        # 只用「具体阶段词 + 技术事实 + 项目/来源」,绝不引入泛化类目词(会稀释排序)。
        # 默认 dry-run;--apply 才写库;只补空描述,不覆盖人工/上游已有描述。
        import json as _j
        lim = int(args[args.index("--limit") + 1]) if "--limit" in args else 0
        # --no-stage:只写技术事实/项目(拉丁+数字),不重复中文阶段词(后者实测会让 r20 掉出容差)
        r = describe_all(dry_run="--apply" not in args, limit=lim,
                         with_stage="--no-stage" not in args)
        print(_j.dumps({k: v for k, v in r.items() if k != "ids"}, ensure_ascii=False))
        if "--samples" in args:
            n = int(args[args.index("--samples") + 1])
            for mid in r["ids"][:n]:
                print("   ", mid, "->", describe_material(mid, dry_run=True))

    elif cmd == "readiness":
        import json as _j
        jid = args[args.index("--job") + 1] if "--job" in args else ""
        r = distribution_readiness(job_id=jid)
        print(_j.dumps(r, ensure_ascii=False, indent=2))

    elif cmd == "langready":
        # 步骤 7:该 job 是否具备点名语种字幕(缺某译文只是「说明」,不整体判死)
        import json as _j
        jid = args[args.index("--job") + 1] if "--job" in args else ""
        lg = args[args.index("--lang") + 1] if "--lang" in args else ""
        r = job_lang_readiness(job_id=jid, lang=lg)
        print(_j.dumps(r, ensure_ascii=False, indent=2))

    elif cmd == "feedback":
        import json as _j
        action = args[args.index("--action") + 1] if "--action" in args else ""
        accept = "--reject" not in args
        note = args[args.index("--note") + 1] if "--note" in args else ""
        lang = args[args.index("--lang") + 1] if "--lang" in args else ""
        orig = args[args.index("--query") + 1] if "--query" in args else ""
        trans = args[args.index("--translated") + 1] if "--translated" in args else ""
        r = log_feedback(action=action, accepted=accept, note=note, lang=lang,
                        orig_query=orig, translated_query=trans)
        # 步骤 10:显式否决某检索词时,把译后词登记为跨语言 veto(后续组装跳过)
        if not accept:
            for _q in (trans or orig,):
                _q = (_q or "").strip()
                if _q:
                    add_veto(_q)
        print(_j.dumps(r, ensure_ascii=False))

    elif cmd == "learning":
        import json as _j
        r = learning_summary(limit=int(args[args.index("--limit") + 1]) if "--limit" in args else 50)
        print(_j.dumps(r, ensure_ascii=False, indent=2))

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

    elif cmd == "metrics":
        # 指标埋点(§8 G 维度):检索/HTTP/MCP 调用数、失败率、零命中率、耗时分位。
        # 内存快照只反映当前进程(独立 CLI 进程必空),故这里从**落盘日志**聚合(跨进程可见)。
        import obs as _obs
        days = int(args[args.index("--days") + 1]) if "--days" in args else 1
        snap = _obs.aggregate_from_logs(days=days)
        src = "logs(last %d day(s))" % days
        if not snap:
            snap = _obs.snapshot()          # 回退:本进程内存指标
            src = "memory(this process)"
        if not snap:
            print("(暂无埋点数据:日志为空且本进程未产生请求。"
                  "服务跑过请求或本进程跑过检索后即可见)")
            return
        print("source: %s" % src)
        print("%-10s %7s %8s %10s %10s %10s" % (
            "kind", "count", "errors", "zero_hits", "ms_p50", "ms_p95"))
        for k, v in sorted(snap.items()):
            print("%-10s %7d %8d %10d %10.2f %10.2f" % (
                k, v["count"], v["errors"], v["zero_hits"],
                v["ms_p50"], v["ms_p95"]))

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

    elif cmd == "package":
        import agent as _agent
        import json as _j
        if "--goal" in args:
            goal = args[args.index("--goal") + 1]
        elif len(args) > 1:
            goal = args[1]
        else:
            goal = ""
        if not goal:
            print("用法: python cli.py package \"目标\" [--kind videos] "
                  "[--scope master|clips|all] [--limit 12]")
            return
        kind = args[args.index("--kind") + 1] if "--kind" in args else ""
        scope = args[args.index("--scope") + 1] if "--scope" in args else "all"
        limit = int(args[args.index("--limit") + 1]) if "--limit" in args else 12
        r = _agent._assemble_package(goal, kind=kind, scope=scope, limit=limit)
        print(_j.dumps({"summary": r["summary"], "path": r["path"],
                        "asset_count": r["asset_count"], "ids": r["ids"]},
                       ensure_ascii=False, indent=2))

    elif cmd == "deliver":
        import agent as _agent
        import json as _j
        confirm = "--confirm" in args
        manifest = args[args.index("--manifest") + 1] if "--manifest" in args else ""
        ids = []
        if "--ids" in args:
            ids = [x.strip() for x in args[args.index("--ids") + 1].split(",") if x.strip()]
        out_dir = args[args.index("--out-dir") + 1] if "--out-dir" in args else None
        fmt = args[args.index("--fmt") + 1] if "--fmt" in args else "mp4"
        res = args[args.index("--res") + 1] if "--res" in args else "720"
        copy_only = "--copy-only" in args
        overwrite = "--overwrite" in args
        if not manifest and not ids:
            print("用法: python cli.py deliver --manifest <pkg.json> [--ids id1,id2] "
                  "[--fmt mp4] [--res 720|1080|0] [--out-dir DIR] "
                  "[--copy-only] [--overwrite] [--confirm]")
            print("  默认 dry-run(不写文件);加 --confirm 才真正导出(写需授权)。")
            return
        r = _agent.core.deliver_package(
            manifest_path=manifest or None, ids=ids or None,
            out_dir=out_dir, confirm=confirm, fmt=fmt, res=res,
            copy_only=copy_only, overwrite=overwrite)
        print(_j.dumps(r, ensure_ascii=False, indent=2))

    elif cmd == "publish":
        import agent as _agent
        import json as _j
        goal = args[args.index("--goal") + 1] if "--goal" in args else (
            args[1] if len(args) > 1 else "")
        if not goal:
            print("用法: python cli.py publish \"目标\" [--confirm] "
                  "[--fmt mp4] [--res 720|1080|0]")
            print("  一键出片:先按目标组装素材包(只读),再导出交付变体;"
                  "默认 dry-run,--confirm 才写文件。")
            return
        confirm = "--confirm" in args
        fmt = args[args.index("--fmt") + 1] if "--fmt" in args else "mp4"
        res = args[args.index("--res") + 1] if "--res" in args else "720"
        pkg = _agent._assemble_package(goal)
        r = _agent.core.deliver_package(
            manifest_path=pkg.get("path"), confirm=confirm, fmt=fmt, res=res)
        print(_j.dumps({"goal": goal, "package": pkg.get("path"),
                        "asset_count": pkg.get("asset_count"),
                        "ids": pkg.get("ids"), "deliver": r},
                       ensure_ascii=False, indent=2))

    elif cmd == "agent":
        import agent
        import json as _json
        if "--status" in args:
            tid = args[args.index("--status") + 1] if len(args) > args.index("--status") + 1 else ""
            print(_json.dumps(agent.agent_status(tid), ensure_ascii=False, indent=2))
            return
        if "--cancel" in args:
            tid = args[args.index("--cancel") + 1] if len(args) > args.index("--cancel") + 1 else ""
            print(_json.dumps(agent.agent_cancel(tid), ensure_ascii=False, indent=2))
            return
        if "--resume" in args:
            tid = args[args.index("--resume") + 1] if len(args) > args.index("--resume") + 1 else ""
            extra = int(args[args.index("--extra-steps") + 1]) if "--extra-steps" in args else 6
            print(_json.dumps(agent.agent_resume(tid, extra_steps=extra), ensure_ascii=False, indent=2))
            return
        if "--list" in args:
            limit = int(args[args.index("--limit") + 1]) if "--limit" in args else 20
            for row in agent.agent_list(limit):
                print("%s  %-18s %2d步  %s  %s"
                      % (row["task_id"], row["status"], row["steps"],
                         row["created_at"], row["task"]))
            return
        if "--cleanup" in args:
            hours = int(args[args.index("--max-age") + 1]) if "--max-age" in args else 72
            print(_json.dumps(agent.agent_cleanup(hours), ensure_ascii=False, indent=2))
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
