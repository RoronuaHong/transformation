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
  python bridge_subtitle.py --watch               # 轮询守护:每 30s 增量登记新素材(Ctrl+C 退出)
  python bridge_subtitle.py --watch --interval 60 --scope batch  # 自定义间隔/范围
"""
import os
import re
import json
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core

DEFAULT_ROOT = os.path.normpath(os.path.join(core.HUB, "..", "subtitle_pipeline"))

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
    added = skipped = 0
    by_kind = {}
    by_type = {}

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


def main():
    args = sys.argv[1:]
    dry = "--dry-run" in args
    watch = "--watch" in args
    rem = [a for a in args if a not in ("--dry-run", "--watch")]
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
        print(f"[watch] 每 {interval}s 增量扫描, Ctrl+C 退出")
        try:
            while True:
                run(scope, root, False, limit)
                time.sleep(interval)
        except KeyboardInterrupt:
            print("\n[watch] 已停止")
        return
    run(scope, root, dry, limit)


if __name__ == "__main__":
    main()
