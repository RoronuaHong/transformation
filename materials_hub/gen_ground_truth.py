"""生成首版 eval_ground_truth.json(人工标注的权威相关集)。

用法: python gen_ground_truth.py
仅读取索引、生成 JSON,不改动任何素材。产出的 gold 是「首版可编辑基线」,
人工可据需增删每类 id。eval_search.py 检测到该文件即改用 human-ground-truth
作为 oracle(R@20 分母=真实相关数,不再被路径子串误报放大)。
"""
import core, json, os

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
with open(out, "w", encoding="utf-8") as f:
    json.dump(gt, f, ensure_ascii=False, indent=2)

print("wrote", out)
for k, v in gt.items():
    print(f"  {k:10}  n={len(v):3}  sample={v[:3]}")
