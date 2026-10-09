"""Materials Hub — subtitle_pipeline 联动桥接(零依赖,只读扫描,不移动文件)。

把 subtitle_pipeline 采集/产出的素材登记进素材中心:按**原路径引用**(不复制,
避免 177 个大视频重复占盘),并自动打标签:
  sp | <platform> | job:<id> | type:<media|subs|notes|benchmark|render|test> | lang:<xx>

素材中心因此对 subtitle_pipeline 的产出是「统一编目 + 可检索 + 可预览」,
而不是再存一份。删除只删索引,不动 subtitle_pipeline 原文件。

用法:
  python bridge_subtitle.py --dry-run           # 只统计,不登记
  python bridge_subtitle.py                       # 登记全部
  python bridge_subtitle.py --scope batch        # 只扫 downloads/batch
  python bridge_subtitle.py --root <SP_ROOT>      # 指定 subtitle_pipeline 根
  python bridge_subtitle.py --limit 20            # 只登记前 20 个(试跑)
  python bridge_subtitle.py --thumbs              # 登记后顺便给新增视频补封面
  python bridge_subtitle.py --embed               # 登记后刷新语义检索索引(增量)
  python bridge_subtitle.py --auto                # 登记后跑 thumb→OCR→shots→pHash(幂等)
  python bridge_subtitle.py --prune               # 顺带清理失效的外部引用(源文件已消失)
  python bridge_subtitle.py --watch               # 轮询守护:每 30s 增量登记新素材(Ctrl+C 退出)
  python bridge_subtitle.py --watch --interval 60 --scope batch  # 自定义间隔/范围
  python bridge_subtitle.py --state               # 只打印上次同步状态

流水线收尾也会自动调用 ``run_job_dir``(见 subtitle_pipeline/discover/hub_push.py);
关同步:环境变量 ``VITUAL_HUB_SYNC=0``。

推荐的"一次跑完"组合(登记 + 自动处理链 + 语义索引 + 巡检):
  python bridge_subtitle.py --auto --embed --prune
  python bridge_subtitle.py --watch --interval 60 --auto --embed --prune

流水线 ``run_job_dir`` 默认会在新增 videos/silent 后:
  1) 对有声 videos 拆 silent+track(``VITUAL_HUB_SPLIT_SILENT=0`` 关)
  2) 跑 ``auto_process``(``VITUAL_HUB_AUTOPROC=0`` 关)
"""
import os
import re
import json
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core

DEFAULT_ROOT = os.path.normpath(os.path.join(core.HUB, "..", "subtitle_pipeline"))
STATE_FILE = os.path.join(core.INDEX_DIR, "bridge_state.json")

# scope -> (相对根的子目录)
SCOPES = {
    "batch": "downloads/batch",
    "benchmarks": "downloads/benchmarks",
    "mode-renders": "downloads/mode-renders",
    "instances": "instances",
}
SKIP_EXT = {".db", ".pyc", ".py", ".log", ".err", ".example"}
# 站点 16 语(与 subtitle_pipeline/langs.py PACKS["site"]、transform/lib/locales.ts 严格同步)。
# `lang:` 受控词表只认这些代码(zh-Hant 保留连字符),ms(马来语)等不在站点集内。
LANGS = {"zh", "zh-Hant", "en", "ja", "ko", "es", "fr", "de", "pt", "ru", "ar", "hi", "id", "vi", "th", "tr"}


def find_meta(path):
    """向上找最近的 fetch_meta.json(通常在 <job>/media/fetch_meta.json)。"""
    d = os.path.dirname(path)
    for _ in range(6):
        if os.path.exists(os.path.join(d, "media", "fetch_meta.json")):
            return os.path.join(d, "media", "fetch_meta.json")
        if os.path.exists(os.path.join(d, "fetch_meta.json")):
            return os.path.join(d, "fetch_meta.json")
        nd = os.path.dirname(d)
        if nd == d:
            break
        d = nd
    return None


def load_meta(path):
    m = find_meta(path)
    if not m:
        return {}
    try:
        with open(m, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def detect_lang(name):
    stem = os.path.splitext(name)[0]
    # 长代码优先(zh-Hant 要先于 zh 匹配);允许语种码位于文件名开头(如 zh.srt)
    for lng in sorted(LANGS, key=len, reverse=True):
        if re.search(r"(?:^|[._\-])" + re.escape(lng) + r"(?:[._\-]|$)", stem, re.IGNORECASE):
            return lng
    return ""


def type_from_rel(rel):
    low = rel.lower().replace("\\", "/")
    if "downloads/batch" in low:
        # 物理交付切片 vs 母版 media(方案 A: type:clip ≠ 新 kind)
        if "/media/clips/" in low:
            return "clip"
        if "/media/" in low:
            return "media"
        if "/subs/" in low:
            return "subs"
        if "/notes/" in low:
            return "notes"
        # 旧返回 batch(scope 名泄漏进 type:);未知路径不打 type,避免脏面
        return ""
    if "benchmarks" in low:
        return "benchmark"
    if "mode-renders" in low:
        return "render"
    if "instances" in low:
        return "test"
    return "other"


def parse_batch_folder(path, root):
    """从 downloads/batch/<platform>_<id>/… 解析 platform / video_id。"""
    try:
        rel = os.path.relpath(path, root).replace("\\", "/")
    except ValueError:
        return "", ""
    m = re.match(r"downloads/batch/([^/]+)/", rel)
    if not m:
        return "", ""
    folder = m.group(1)
    if "_" in folder:
        plat, vid = folder.split("_", 1)
        return plat, vid
    return "", folder


def file_tags_and_desc(path, root, *, platform="", video_id="", title=""):
    """为单个文件生成受控标签 + 描述(fetch_meta 优先,缺省回落到 batch 目录名)。"""
    rel = os.path.relpath(path, root)
    meta = load_meta(path)
    t = type_from_rel(rel)
    plat_fb, vid_fb = parse_batch_folder(path, root)
    tags = ["sp"]
    plat = (platform or meta.get("platform") or plat_fb or "").strip()
    vid = (video_id or meta.get("id") or vid_fb or "").strip()
    if plat:
        tags.append(plat)
    if vid:
        tags.append("job:" + vid)
    if t:
        tags.append("type:" + t)
    kind = core.classify(path)
    tags.extend(core.media_facet_tags(path, kind))
    lng = detect_lang(os.path.basename(path))
    if lng:
        tags.append("lang:" + lng)
    desc = (title or meta.get("title") or "").strip()
    return ",".join(tags), desc, t


def _iter_files(base):
    for dirpath, _, files in os.walk(base):
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in SKIP_EXT:
                continue
            if f.startswith("_probe"):
                continue
            yield os.path.join(dirpath, f)


def _register_tree(base, root, dry_run, limit, *, platform="", video_id="", title=""):
    """登记 base 目录树。返回统计 dict。"""
    added = skipped = rejected = 0
    by_kind = {}
    by_type = {}
    new_ids = []
    for p in _iter_files(base):
        tags, desc, t = file_tags_and_desc(
            p, root, platform=platform, video_id=video_id, title=title
        )
        kind = core.classify(p)
        if dry_run:
            added += 1
            by_kind[kind] = by_kind.get(kind, 0) + 1
            by_type[t] = by_type.get(t, 0) + 1
            if limit and added >= limit:
                break
            continue
        r = core.ingest_external(
            p, source="subtitle_pipeline", tags=tags, description=desc
        )
        if not r:
            continue
        if r.get("status") == "skipped":
            skipped += 1
            continue
        if r.get("status") == "rejected":
            rejected += 1
            continue
        if r["status"] == "added":
            added += 1
            new_ids.append(r["id"])
            by_kind[kind] = by_kind.get(kind, 0) + 1
            by_type[t] = by_type.get(t, 0) + 1
        else:
            skipped += 1
        if limit and added >= limit:
            break
    return {
        "added": added,
        "skipped": skipped,
        "rejected": rejected,
        "by_kind": by_kind,
        "by_type": by_type,
        "new_ids": new_ids,
        "dry_run": dry_run,
    }


def run_job_dir(job_dir, root=None, dry_run=False, *, platform="", video_id="", title=""):
    """登记单个 batch 任务目录(流水线收尾钩子用)。幂等(sha256 去重)。"""
    root = os.path.abspath(root or DEFAULT_ROOT)
    job_dir = os.path.abspath(job_dir)
    if not os.path.isdir(job_dir):
        return {"added": 0, "skipped": 0, "rejected": 0, "by_kind": {},
                "by_type": {}, "new_ids": [], "dry_run": dry_run,
                "error": f"not a dir: {job_dir}"}
    core.init_hub()
    st = _register_tree(
        job_dir, root, dry_run, 0,
        platform=platform, video_id=video_id, title=title or "",
    )
    st["scope"] = "job_dir"
    st["job_dir"] = job_dir
    print(
        f"\n{'[DRY-RUN] ' if dry_run else ''}job_dir 登记/规划 {st['added']} 个"
        + (f", 跳过重复 {st['skipped']} 个" if not dry_run else "")
        + (f", 拒绝 {st['rejected']} 个" if st.get("rejected") else "")
    )
    print("  按种类:", st["by_kind"])
    print("  按类型:", st["by_type"])
    if not dry_run:
        write_state(st)
        # 方案 A: clip/组件 → parent:<母版>
        link = core.link_relation_parents(mids=st.get("new_ids") or [])
        st["transcripts"] = core.attach_job_transcripts()
        st["link_parents"] = link
        if link.get("linked"):
            print(f"[link-parents] linked={link['linked']} "
                  f"clips={link.get('clips')} components={link.get('components')}")
        write_state(st)
        # P2-B: 有声 videos → silent + track(关: VITUAL_HUB_SPLIT_SILENT=0)
        if os.environ.get("VITUAL_HUB_SPLIT_SILENT", "1").strip().lower() not in (
            "0", "false", "no", "off",
        ):
            # split_new_videos 内部已跳过 role:clip
            spl = core.split_new_videos(st.get("new_ids") or [])
            st["split"] = {"ran": spl["ran"], "ok": spl["ok"],
                           "new_ids": spl.get("new_ids") or []}
            if spl.get("new_ids"):
                st.setdefault("new_ids", []).extend(spl["new_ids"])
                print(f"[split] ok={spl['ok']}/{spl['ran']} "
                      f"new={len(spl['new_ids'])}")
                # 新 silent/audio 再挂 parent
                link2 = core.link_component_parents()
                st["link_parents_after_split"] = link2
            write_state(st)
        # 事件驱动:新入库的画面类素材自动跑 thumb→OCR→shots→pHash(关:VITUAL_HUB_AUTOPROC=0)
        if os.environ.get("VITUAL_HUB_AUTOPROC", "1").strip().lower() not in (
            "0", "false", "no", "off",
        ):
            st["autoproc"] = do_auto(st.get("new_ids") or [])
    return st


def run(scope, root, dry_run, limit):
    """扫描并登记。返回统计 dict(含本轮新增的 id,供后续补封面)。"""
    added = skipped = 0
    by_kind = {}
    by_type = {}
    new_ids = []
    remain = limit

    for sc_name, sub in SCOPES.items():
        if scope != "all" and sc_name != scope:
            continue
        base = os.path.join(root, sub)
        if not os.path.isdir(base):
            print(f"[skip] {sub} 不存在")
            continue
        part = _register_tree(base, root, dry_run, remain)
        added += part["added"]
        skipped += part["skipped"]
        new_ids.extend(part["new_ids"])
        for k, v in part["by_kind"].items():
            by_kind[k] = by_kind.get(k, 0) + v
        for k, v in part["by_type"].items():
            by_type[k] = by_type.get(k, 0) + v
        if limit:
            remain = max(0, limit - added)
            if remain == 0:
                break

    print(f"\n{'[DRY-RUN] ' if dry_run else ''}scope={scope}  登记/规划 {added} 个"
          + (f", 跳过重复 {skipped} 个" if not dry_run else ""))
    print("  按种类:", by_kind)
    print("  按类型:", by_type)
    return {"scope": scope, "added": added, "skipped": skipped,
            "by_kind": by_kind, "by_type": by_type, "new_ids": new_ids,
            "dry_run": dry_run}


def do_auto(new_ids):
    """对新入库(及仍有缺项)的 videos/silent/images 跑 auto_process 链。幂等。"""
    ids = []
    for mid in new_ids or []:
        m = core.get_material(mid)
        if m and m.get("kind") in ("videos", "silent", "images"):
            ids.append(mid)
    # 顺带补历史缺项(上限 8,避免一次扫全库过久)
    for m in core.all_materials():
        if len(ids) >= 8:
            break
        if m.get("kind") not in ("videos", "silent", "images"):
            continue
        if m["id"] in ids:
            continue
        if any(core.pending_processing(m["id"]).values()):
            ids.append(m["id"])
    if not ids:
        print("[auto] 无需处理")
        return {"processed": 0}
    ok = 0
    for mid in ids:
        try:
            core.auto_process_material(mid)
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"[auto] {mid} fail: {type(e).__name__}: {e}")
    print(f"[auto] processed {ok}/{len(ids)}")
    return {"processed": ok, "ids": ids}


def do_thumbs(new_ids):
    """给新增视频补封面(顺带补齐历史缺失项)。无 ffmpeg 时说明并跳过。"""
    if not core.thumbs_status()["ffmpeg"]:
        print("[thumbs] 未找到 ffmpeg,跳过(面板会自动退化为浏览器截帧)")
        return 0
    ids = [i for i in new_ids if (core.get_material(i) or {}).get("kind") in ("videos", "silent")]
    ids += [i for i in core.missing_thumbnail_ids() if i not in ids]
    if not ids:
        print("[thumbs] 无需生成")
        return 0
    made = fail = 0
    for n, mid in enumerate(ids, 1):
        ok = core.make_thumb(mid)
        made += bool(ok)
        fail += not ok
        if n % 10 == 0 or n == len(ids):
            print(f"[thumbs] {n}/{len(ids)} ok={made} fail={fail}", flush=True)
    return made


def do_prune(dry_run=False):
    """巡检并(可选)清理失效的外部引用。只删索引,绝不触碰磁盘文件。"""
    bad = core.broken_externals()
    if not bad:
        print("[prune] 引用完整,无失效项")
        return 0
    print(f"[prune] 失效外部引用 {len(bad)} 条(原文件已不存在):")
    for m in bad[:10]:
        print(f"   - {m['name']}  <-  {m.get('external_path','')}")
    if len(bad) > 10:
        print(f"   …其余 {len(bad) - 10} 条")
    if dry_run:
        print("[prune] dry-run,未清理")
        return 0
    core.prune_broken_externals()
    print(f"[prune] 已清理 {len(bad)} 条索引(原文件未触碰)")
    return len(bad)


def do_embed():
    """增量刷新语义检索索引(新登记的素材立刻可被自然语言搜到)。"""
    info = core.embed_status()
    if not info["available"]:
        print("[embed] 未发现本地 embedding 模型,跳过(可用 cli.py embed 单独检查)")
        return 0
    r = core.build_embeddings()
    print("[embed] 模型 %s:本次索引 %d 条(共 %s)" %
          (info["model"], r.get("embedded", 0), core.embed_status()["embedded"]))
    return r.get("embedded", 0)


def write_state(st):
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"last_sync": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "stats": st, "external": core.external_stats()},
                      f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def print_state():
    if not os.path.exists(STATE_FILE):
        print("尚无同步记录(还没跑过 bridge)")
        return
    with open(STATE_FILE, encoding="utf-8") as f:
        print(json.dumps(json.load(f), ensure_ascii=False, indent=2))


def sync_once(scope, root, dry, limit, want_thumbs, want_prune, want_embed=False,
              want_auto=False):
    """一轮完整同步:登记 → 关系回填 → (auto 或 thumbs) → 语义索引 → 引用巡检 → 记录状态。"""
    st = run(scope, root, dry, limit)
    if not dry:
        link = core.link_relation_parents(mids=st.get("new_ids") or [])
        st["transcripts"] = core.attach_job_transcripts()
        st["link_parents"] = link
        if link.get("linked"):
            print(f"[link-parents] linked={link['linked']}")
        if os.environ.get("VITUAL_HUB_SPLIT_SILENT", "1").strip().lower() not in (
            "0", "false", "no", "off",
        ):
            spl = core.split_new_videos(st.get("new_ids") or [])
            st["split"] = {"ran": spl["ran"], "ok": spl["ok"],
                           "new_ids": spl.get("new_ids") or []}
            if spl.get("new_ids"):
                st.setdefault("new_ids", []).extend(spl["new_ids"])
                core.link_component_parents()
                print(f"[split] ok={spl['ok']}/{spl['ran']}")
    if want_auto and not dry:
        st["autoproc"] = do_auto(st["new_ids"])
    elif want_thumbs and not dry:
        st["thumbs_made"] = do_thumbs(st["new_ids"])
    if want_embed and not dry:
        st["embedded"] = do_embed()
    if want_prune:
        st["pruned"] = do_prune(dry)
    if not dry:
        write_state(st)
    return st


def main():
    args = sys.argv[1:]
    dry = "--dry-run" in args
    watch = "--watch" in args
    thumbs = "--thumbs" in args
    prune = "--prune" in args
    embed = "--embed" in args
    auto = "--auto" in args
    if "--state" in args:
        print_state()
        return
    flags = ("--dry-run", "--watch", "--thumbs", "--prune", "--embed", "--auto")
    rem = [a for a in args if a not in flags]
    scope, root, limit, interval = "all", DEFAULT_ROOT, 0, 30
    i = 0
    while i < len(rem):
        a = rem[i]
        if a == "--scope" and i + 1 < len(rem):
            scope = rem[i + 1]; i += 2
        elif a == "--root" and i + 1 < len(rem):
            root = rem[i + 1]; i += 2
        elif a == "--limit" and i + 1 < len(rem):
            limit = int(rem[i + 1]); i += 2
        elif a == "--interval" and i + 1 < len(rem):
            interval = int(rem[i + 1]); i += 2
        else:
            i += 1
    core.init_hub()
    print("subtitle_pipeline 根:", root)
    if watch:
        print(f"[watch] 每 {interval}s 增量同步(登记"
              + ("+auto" if auto else "")
              + ("+封面" if thumbs and not auto else "")
              + ("+语义" if embed else "")
              + ("+巡检" if prune else "") + "), Ctrl+C 退出")
        try:
            while True:
                sync_once(scope, root, False, limit, thumbs, prune, embed, auto)
                time.sleep(interval)
        except KeyboardInterrupt:
            print("\n[watch] 已退出")
            return
    sync_once(scope, root, dry, limit, thumbs, prune, embed, auto)


if __name__ == "__main__":
    main()
