# -*- coding: utf-8 -*-
"""OCR 独立 runner:由 core.ocr_material 以 **subtitle_pipeline 的 venv python** 子进程调用
(该 venv 已装 rapidocr-onnxruntime,离线推理;materials_hub 自身保持零依赖)。

用法:
    <sp-venv-python> ocr_runner.py img1.jpg img2.jpg ...
stdout 输出单个 JSON 数组: [{"file": "...", "text": "行1\\n行2",
                            "lines": [{"text","box":[[x,y]*4],"score"}]}, ...]
单张图失败不中断整体,text 为空串;引擎不可用则输出 {"error": ...}。
"""
import json
import sys

# 关键:管道下 Python 默认用 locale 编码(中文 Windows=GBK)写 stdout,
# 父进程按 UTF-8 解码会得到乱码;这里强制 UTF-8(双保险:父进程还会注入 PYTHONIOENCODING)。
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def _norm_box(box):
    """rapidocr 的检测框统一成 [[x,y]*4]（兼容 4 点或 8 个平铺数两种形态）。"""
    try:
        return [[round(float(box[i][0]), 1), round(float(box[i][1]), 1)]
                for i in range(4)]
    except Exception:
        pass
    try:
        v = [float(x) for x in box]
        if len(v) == 8:
            return [[round(v[i], 1), round(v[i + 1], 1)] for i in range(0, 8, 2)]
    except Exception:
        pass
    return []


def main():
    try:
        from rapidocr_onnxruntime import RapidOCR  # noqa: 仅在 SP venv 内可导入
        engine = RapidOCR()
    except Exception as e:                       # 引擎缺失/损坏 → 明确报错
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False))
        return
    out = []
    for p in sys.argv[1:]:
        try:
            res, _ = engine(p)
            lines = []
            for it in (res or []):
                if len(it) >= 2 and it[1] and str(it[1]).strip():
                    lines.append({
                        "text": str(it[1]).strip(),
                        "box": _norm_box(it[0]),
                        "score": (float(it[2])
                                  if len(it) >= 3 and it[2] is not None
                                  else None),
                    })
        except Exception as e:
            out.append({"file": p, "text": "", "lines": [], "error": str(e)[:120]})
            continue
        out.append({"file": p,
                    "text": "\n".join(l["text"] for l in lines),
                    "lines": lines})
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
