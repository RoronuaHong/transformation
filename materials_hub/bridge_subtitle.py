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
  python bridge_subtitle.py --prune               # 顺带清理失效的外部引用(源文件已消失)
  python bridge_subtitle.py --watch               # 轮询守护:每 30s 增量登记新素材(Ctrl+C 退出)
  python bridge_subtitle.py --watch --interval 60 --scope batch  # 自定义间隔/范围
  python bridge_subtitle.py --state               # 只打印上次同步状态

推荐的"一次跑完"组合(登记 + 封面 + 语义索引 + 巡检):
  python bridge_subtitle.py --thumbs --embed --prune
  python bridge_subtitle.py --watch --interval 60 --thumbs --embed --prune
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
LANGS = {"zh", "en", "ja", "ko", "ru", "fr", "de", "es", "pt", "ar", "th", "vi", "id", "ms"}


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
    for lng in LANGS:
        if re.search(r"[._\-]" + lng + r"([._\-]|$)", stem, re.IGNORECASE):
            return lng
    return ""


def type_from_rel(rel):
    low = rel.lower().replace("\\", "/")
    if "downloads/batch" in low:
        if "/media/" in low:
            return "media"
        if "/subs/" in low:
            return "subs"
        if "/notes/" in low:
            return "notes"
        return "batch"
    if "benchmarks" in low:
        return "benchmark"
    if "mode-renders" in low:
        return "render"
    if "instances" in low:
        return "test"
    return "other"


def run(scope, root, dry_run, limit):
    """扫描并登记。返回统计 dict(含本轮新增的 id,供后续补封面)。"""
    added = skipped = 0
    by_kind = {}
    by_type = {}
    new_ids = []

    for sc_name, sub in SCOPES.items():
        if scope != "all" and sc_name != scope:
            continue
        base = os.path.join(root, sub)
        if not os.path.isdir(base):
            print(f"[skip] {sub} 不存在")
            continue
        for dirpath, _, files in os.walk(base):
            for f in files:
                ext = os.path.splitext(f)[1].lower()
                if ext in SKIP_EXT:
                    continue
                p = os.path.join(dirpath, f)
                rel = os.path.relpath(p, root)
                meta = load_meta(p)
                t = type_from_rel(rel)
                tags = ["sp"]
                if meta.get("platform"):
                    tags.append(meta["platform"])
                if meta.get("id"):
                    tags.append("job:" + meta["id"])
                tags.append("type:" + t)
                lng = detect_lang(f)
                if lng:
                    tags.append("lang:" + lng)
                desc = meta.get("title", "")
                kind = core.classify(p)
                if dry_run:
                    added += 1
                    by_kind[kind] = by_kind.get(kind, 0) + 1
                    by_type[t] = by_type.get(t, 0) + 1
                    continue
                r = core.ingest_external(p, source="subtitle_pipeline",
                                         tags=",".join(tags), description=desc)
                if not r:
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
            if limit and added >= limit:
                break
        if limit and added >= limit:
            break

    print(f"\n{'[DRY-RUN] ' if dry_run else ''}scope={scope}  登记/规划 {added} 个"
          + (f", 跳过重复 {skipped} 个" if not dry_run else ""))
    print("  按种类:", by_kind)
    print("  按类型:", by_type)
    return {"scope": scope, "added": added, "skipped": skipped,
            "by_kind": by_kind, "by_type": by_type, "new_ids": new_ids,
            "dry_run": dry_run}


def do_thumbs(new_ids):
    """给新增视频补封面(顺带补齐历史缺失项)。无 ffmpeg 时说明并跳过。"""
    if not core.thumbs_status()["ffmpeg"]:
        print("[thumbs] 未找到 ffmpeg,跳过(面板会自动退化为浏览器截帧)")
        return 0
    ids = [i for i in new_ids if (core.get_material(i) or {}).get("kind") == "videos"]
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


def sync_once(scope, root, dry, limit, want_thumbs, want_prune, want_embed=False):
    """一轮完整同步:登记 → 补封面 → 语义索引 → 引用巡检 → 记录状态。"""
    st = run(scope, root, dry, limit)
    if want_thumbs and not dry:
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
    if "--state" in args:
        print_state()
        return
    flags = ("--dry-run", "--watch", "--thumbs", "--prune", "--embed")
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
              + ("+封面" if thumbs else "") + ("+语义" if embed else "")
              + ("+巡检" if prune else "") + "), Ctrl+C 退出")
        try:
            while True:
                sync_once(scope, root, False, limit, thumbs, prune, embed)
                time.sleep(interval)
        except KeyboardInterrupt:
            print("\n[watch] 已停止")
        return
    sync_once(scope, root, dry, limit, thumbs, prune, embed)


if __name__ == "__main__":
    main()
