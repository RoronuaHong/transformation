# -*- coding: utf-8 -*-
"""画面描述独立 runner:由 core.visual_material 以 **materials_hub 自身 python** 子进程调用
(VLM 推理走本地 ollama 的 gemma4:e2b 多模态模型,零依赖:只用标准库 urllib 打 /api/generate;
materials_hub 保持零第三方依赖)。

用法:
    <python> visual_runner.py img1.jpg img2.jpg ...
stdout 输出单个 JSON 数组: [{"file": "...", "description": "...", "tags": "...", "text": "..."}, ...]
单张图失败不中断整体,text 为空串;引擎/服务不可用则输出 {"error": ...}。

提示:gemma4 带 thinking,可能在前置输出推理过程;解析时只取「描述:」「标签:」标记的段落,
对自由前缀/推理噪声鲁棒。
"""
import base64
import json
import os
import sys
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

MODEL = os.environ.get("VITUAL_VISUAL_MODEL", "gemma4:e2b").strip()
OLLAMA_URL = os.environ.get("VITUAL_OLLAMA_URL", "http://localhost:11434").rstrip("/")
# 双语输出(对齐最佳实践的多语言检索):中文描述/标签走词法 bigram 与中文查询直接命中;
# EN描述/EN标签供英文查询(及 bge-m3 英文稠密通道)直接命中,无需全依赖翻译桥。两者都落
# sidecar + 0.35 低权重通道,不写 description,避免稀释主排序(同 OCR/视觉铁律)。
PROMPT = (
    "请观察这张图片,用简体中文和英文分别描述,严格按以下 4 行格式输出,不要输出多余内容:\n"
    "描述: <一句话点明画面主体。若是食物,必须写出具体菜名或食材,例如红烧鸡翅、排骨、玉米,"
    "禁止只写食品、零食、糖果、物体、物品>\n"
    "标签: <3到8个中文关键词,逗号分隔,必须包含可检索的具体物体名>\n"
    "EN描述: <one English sentence naming the specific subject, e.g. braised chicken wings, not just food>\n"
    "EN标签: <3 to 8 English keywords, comma-separated, include the specific object name>"
)


def _b64(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


def _describe(path):
    payload = json.dumps({
        "model": MODEL,
        "prompt": PROMPT,
        "images": [_b64(path)],
        "stream": False,
        "temperature": 0.2,
        # gemma4 默认先写一段思考。思考里常出现「请提供图片」这类拒答句,
        # 会盖住后面真正的画面描述。关掉思考,让输出停在 4 行格式上。
        "think": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_URL + "/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("response", "")


def _field_value(line):
    return line.split(":", 1)[-1].split("：", 1)[-1].strip()


def _is_refusal(text):
    """思考过程里的拒答句,不能当成画面描述。"""
    s = text or ""
    return any(k in s for k in ("没有提供", "未提供", "无法观察", "无法进行描述", "请上传", "请提供"))


def _parse(text):
    """从模型输出中解析 描述 / 标签 / EN描述 / EN标签。对 thinking 前缀与自由格式鲁棒。

    同一字段可能出现多次(思考里先拒答,后面才是真描述)。只保留最后一条非拒答。
    """
    description, tags, en_description, en_tags = "", "", "", ""
    for line in (text or "").splitlines():
        s = line.strip()
        low = s.lower()
        if low.startswith("描述") or (low.startswith("description") and "en" not in low):
            val = _field_value(s)
            if val and not _is_refusal(val):
                description = val
        elif low.startswith("标签") or low.startswith("tags") or low.startswith("关键字") or low.startswith("关键词"):
            val = _field_value(s)
            if val and not _is_refusal(val):
                tags = val
        elif low.startswith("en描述") or low.startswith("en 描述") or low.startswith("en-description") \
                or low.startswith("description(en)") or low.startswith("en description"):
            val = _field_value(s)
            if val and not _is_refusal(val):
                en_description = val
        elif low.startswith("en标签") or low.startswith("en 标签") or low.startswith("en-tags") \
                or low.startswith("tags(en)") or low.startswith("en tags"):
            val = _field_value(s)
            if val and not _is_refusal(val):
                en_tags = val
    if not description and not tags and not en_description and not en_tags:
        # 没有结构化标记:整段当作描述。拒答句不当描述,否则会写进可检索 sidecar。
        raw = (text or "").strip()
        if raw and not _is_refusal(raw):
            description = raw
    return description, tags, en_description, en_tags


def main():
    files = sys.argv[1:]
    if not files:
        print(json.dumps([]))
        return
    out = []
    for p in files:
        try:
            raw = _describe(p)
            description, tags, en_description, en_tags = _parse(raw)
        except Exception as e:
            out.append({"file": p, "description": "", "tags": "", "en_description": "",
                        "en_tags": "", "text": "", "error": str(e)[:160]})
            continue
        text = "\n".join(x for x in [
            f"描述: {description}" if description else "",
            f"标签: {tags}" if tags else "",
            f"EN描述: {en_description}" if en_description else "",
            f"EN标签: {en_tags}" if en_tags else "",
        ] if x)
        out.append({"file": p, "description": description, "tags": tags,
                    "en_description": en_description, "en_tags": en_tags, "text": text})
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
