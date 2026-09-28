"""生成 eval_ground_truth.json(人工标注的权威相关集)。

用法:
    python gen_ground_truth.py           # 合并模式:只**新增**缺失的查询键,
                                         # 绝不覆盖已有键(保护人工核对过的标注)
    python gen_ground_truth.py --force   # 重建模式:丢弃现文件,只写本脚本内置的 9 类种子

仅读取索引、生成 JSON,不改动任何素材。eval_search.py 检测到该文件即改用
human-ground-truth 作为 oracle(R@20 分母=真实相关数)。
2026-09-28 起该文件含 7 组人工核对的窄查询(见 §15.4),默认合并防止重跑覆盖。
"""
import sys

import core, json, os

FORCE = "--force" in sys.argv

core.init_hub()
ms = core.all_materials()


def blob(m):
    return " ".join(str(m.get(k, "")) for k in
                    ("name", "tags", "description", "external_path", "rel_path")).lower()


def pick(*subs):
    """素材文本里命中任一子串即视为相关(首版粗标,可调)。"""
    def pred(m):
        b = blob(m)
        return any(s in b for s in subs)
    return [m["id"] for m in ms if pred(m)]


gt = {
    "去除字幕只留背景": pick("dehardsub", "hardsub"),
    "把模糊画面变清晰": pick("deblur", "clean"),
    "按时间切的片段":   pick("segment"),
    "字幕文本文件":     pick("srt", "ass", "subs", "type:subs"),
    "对比渲染结果":     pick("cmp", "compare"),
    "人脸修复":         pick("codeformer", "gfpgan", "face", "fixed"),
    "基准测试视频":     pick("benchmark", "bench"),
    "b站下载的字幕":    [m["id"] for m in ms
                         if "bilibili" in blob(m)
                         and any(t in blob(m) for t in ("srt", "ass", "subs", "type:subs", "caption"))],
    "修好的成品":       pick("out", "final", "fixed"),
}

out = os.path.join(os.path.dirname(__file__), "eval_ground_truth.json")

old = {}
if os.path.exists(out):
    try:
        with open(out, encoding="utf-8") as f:
            old = json.load(f)
    except Exception as e:
        print(f"[warn] 现文件不可读({e}),按 --force 语义重建")
        old = {}

if FORCE or not old:
    merged = dict(gt)
    mode = "force-rebuild" if FORCE else "init"
else:
    merged = dict(old)
    added = [k for k in gt if k not in merged]
    for k in added:
        merged[k] = gt[k]
    mode = f"merge(+{len(added)} new, kept {len(old)} existing)"

with open(out, "w", encoding="utf-8") as f:
    json.dump(merged, f, ensure_ascii=False, indent=2)

print(f"wrote {out}  (mode={mode}, total {len(merged)} queries)")
for k, v in merged.items():
    print(f"  {k:14}  n={len(v):3}  sample={v[:3]}")
