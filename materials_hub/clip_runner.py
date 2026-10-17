# -*- coding: utf-8 -*-
"""CLIP 独立 runner:由 materials_hub.core 以 **subtitle_pipeline venv python** 子进程调用
(该 venv 装 open_clip_torch + torch;hub 自身保持零依赖)。

用法:
  <sp-python> clip_runner.py probe
  <sp-python> clip_runner.py embed-image img1.jpg img2.jpg ...
  <sp-python> clip_runner.py embed-text "a cat" "厨房"

stdout 单行 JSON:
  probe → {"ok":true,"model":"ViT-B-32/openai","dim":512} | {"ok":false,"error":...}
  embed-* → {"model":"...","dim":N,"vectors":[[...],...]} | {"error":...}

权重缓存:环境变量 VITUAL_CLIP_CACHE / OPEN_CLIP_CACHE_DIR,默认
materials_hub/models/clip。首次需联网下载(~350MB ViT-B-32)。
"""
from __future__ import annotations

import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# 默认模型(2026-10-15 恢复 SigLIP2-256/webli,768 维):
# 2026-10-14 曾因 SigLIP2 safetensors ~1.5GB(fp32)在 **CPU 侧**加载触发 Windows
# ERROR_NOT_ENOUGH_MEMORY(1455)/段错误而回退 ViT-B-32。2026-10-15 定位:那只是
# **CPU 提交内存尖峰**;把 device 传进 create_model_and_transforms 让权重**直接加载到
# GPU**(RTX2060 6G 放得下,实测 cuda 直载 1.5GB OK)即可绕开,无需加内存。tokenizer 的
# 离线问题也从本地 snapshot 目录建 HFTokenizer 解决(读 tokenizer_config.json,绕开缺
# config.json 的负缓存)。SigLIP2 比 ViT-B-32 图文检索高约 8-10 点,且 build_auto_tags
# 本就按 SigLIP2 zero-shot 设计。轻量回退(内存紧/无 GPU 时):
#   VITUAL_CLIP_MODEL=ViT-B-32 VITUAL_CLIP_PRETRAINED=openai(单体 .pt,内置 tokenizer)
_MODEL_NAME = os.environ.get("VITUAL_CLIP_MODEL", "ViT-B-16-SigLIP2-256").strip() or "ViT-B-16-SigLIP2-256"
_PRETRAINED = os.environ.get("VITUAL_CLIP_PRETRAINED", "webli").strip() or "webli"
_CACHE = (
    os.environ.get("VITUAL_CLIP_CACHE", "").strip()
    or os.environ.get("OPEN_CLIP_CACHE_DIR", "").strip()
    or os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "clip")
)

_STATE = {"model": None, "preprocess": None, "tokenizer": None, "device": "cpu"}

# HF 访问策略(2026-10-10):hub 是全离线系统,HF 直连在本机 10060 超时;
# 已有本地缓存时强制 HF_HUB_OFFLINE=1(必须在 import transformers/open_clip 之前设,
# huggingface_hub 在 import 时读取该常量);首次下载走 hf-mirror。
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
# HF 缓存统一到 models/clip:open_clip 只预下权重,tokenizer 文件是 get_tokenizer 阶段
# transformers 按需拉的;不指 HF_HUB_CACHE 的话会落 ~/.cache 且与权重缓存分家,
# 离线机器上 tokenizer 永远找不到(实测 10060 超时假死)。
os.environ.setdefault("HF_HUB_CACHE", _CACHE)


def _local_weight():
    """优先用已下载的本地 .pt(避免每次走 HF/代理)。
    文件名**必须跟模型名走**(如 ViT-B-16-SigLIP2-256.pt):曾踩坑——硬编码 ViT-B-32.pt
    会被塞进 SigLIP2 架构,报 "text pos_embed width changed"。"""
    env = (os.environ.get("VITUAL_CLIP_WEIGHTS") or "").strip()
    if env and os.path.isfile(env):
        return env
    cand = os.path.join(_CACHE, _MODEL_NAME + ".pt")
    if os.path.isfile(cand) and os.path.getsize(cand) > 10_000_000:
        return cand
    return None


def _hf_cache_hit():
    """open_clip 经 HF 下载的权重是否已在本地缓存(models--timm--<模型名> 目录)。"""
    import glob
    return bool(glob.glob(os.path.join(_CACHE, "models--*--" + _MODEL_NAME)))


# 断网决策必须在**模块级、任何 transformers/huggingface_hub import 之前**:
# huggingface_hub.constants 在 import 时读取 HF_HUB_OFFLINE,事后设置无效。
_allow_dl = os.environ.get("VITUAL_CLIP_ALLOW_DOWNLOAD", "").strip().lower() in (
    "1", "true", "yes", "on")
if _hf_cache_hit() and not _allow_dl:
    os.environ["HF_HUB_OFFLINE"] = "1"


def _out(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def _hf_snapshot_dir():
    """SigLIP2 等经 HF 缓存的本地 snapshot 目录(含 tokenizer.json/tokenizer_config.json)。"""
    import glob
    hits = glob.glob(os.path.join(_CACHE, "models--*--" + _MODEL_NAME, "snapshots", "*"))
    return hits[0] if hits else None


def _build_tokenizer(model):
    """构建分词器,离线友好。
    - HF tokenizer 模型(如 SigLIP2/Gemma):从本地 snapshot 目录建 HFTokenizer,
      规避离线时 AutoTokenizer 因缺 config.json(负缓存 .no_exist)而失败;
    - 其它(如 ViT-B-32/openai 的内置 SimpleTokenizer):走 open_clip.get_tokenizer。"""
    import open_clip
    ctx = getattr(model, "context_length", 0) or 64
    if "siglip" in _MODEL_NAME.lower():
        snap = _hf_snapshot_dir()
        if snap:
            from open_clip.tokenizer import HFTokenizer
            return HFTokenizer(snap, context_length=ctx)
    return open_clip.get_tokenizer(_MODEL_NAME, context_length=ctx)


def _load():
    if _STATE["model"] is not None:
        return
    import open_clip
    import torch

    os.makedirs(_CACHE, exist_ok=True)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    weights = _local_weight()
    pretrained = weights if weights else _PRETRAINED
    # PyTorch 2.6+ 默认 weights_only=True, OpenAI 官方 .pt 是 TorchScript 式 archive
    # 会拒绝加载;本地信任源权重时临时放宽(仅本进程、仅加载瞬间)。
    _orig_load = torch.load

    def _torch_load_compat(*a, **kw):
        kw["weights_only"] = False
        return _orig_load(*a, **kw)

    torch.load = _torch_load_compat
    try:
        # 2026-10-15:传 device 让权重直接加载到目标设备(GPU)。SigLIP2 safetensors 1.5GB
        # 若在 CPU 侧加载会触发 Windows 提交内存 1455/段错误;GPU 6G 放得下。
        model, _, preprocess = open_clip.create_model_and_transforms(
            _MODEL_NAME, pretrained=pretrained, cache_dir=_CACHE, device=device,
        )
    finally:
        torch.load = _orig_load
    model = model.to(device).eval()
    _STATE["model"] = model
    _STATE["preprocess"] = preprocess
    _STATE["tokenizer"] = _build_tokenizer(model)
    _STATE["device"] = device
    tag = "%s/%s" % (_MODEL_NAME, "local" if weights else _PRETRAINED)
    _STATE["tag"] = tag
    _STATE["weights"] = weights or ""


def probe():
    try:
        import open_clip  # noqa: F401
    except Exception as e:
        return {"ok": False, "error": "no_open_clip:%s" % e}
    weights = _local_weight()
    allow_dl = os.environ.get("VITUAL_CLIP_ALLOW_DOWNLOAD", "").strip().lower() in (
        "1", "true", "yes", "on",
    )
    if not weights and not _hf_cache_hit() and not allow_dl:
        return {
            "ok": False,
            "error": "no_local_weights",
            "cache": _CACHE,
            "hint": (
                "权重未就位:本地 .pt(%s)或 HF 缓存(models--timm--%s)均无;"
                "首次下载可设 VITUAL_CLIP_ALLOW_DOWNLOAD=1"
                % (os.path.join(_CACHE, _MODEL_NAME + ".pt"), _MODEL_NAME)
            ),
        }
    try:
        _load()
        import torch
        with torch.no_grad():
            t = _STATE["tokenizer"](["ping"])
            v = _STATE["model"].encode_text(t.to(_STATE["device"]))
            dim = int(v.shape[-1])
        return {"ok": True, "model": _STATE["tag"], "dim": dim,
                "device": _STATE["device"], "cache": _CACHE,
                "weights": _STATE.get("weights") or ""}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e),
                "cache": _CACHE, "hint": "首次需联网下载权重到 cache"}


def embed_images(paths):
    from PIL import Image
    import torch

    _load()
    vecs = []
    model, prep, device = _STATE["model"], _STATE["preprocess"], _STATE["device"]
    for p in paths:
        try:
            im = Image.open(p).convert("RGB")
            tens = prep(im).unsqueeze(0).to(device)
            with torch.no_grad():
                v = model.encode_image(tens)
                v = v / v.norm(dim=-1, keepdim=True)
            vecs.append([float(x) for x in v[0].detach().cpu().tolist()])
        except Exception as e:
            vecs.append(None)
            # 占位:父进程按 index 对齐;错误信息塞到旁路字段
            if "_errors" not in _STATE:
                _STATE["_errors"] = {}
            _STATE["_errors"][p] = str(e)[:160]
    out = {"model": _STATE["tag"], "dim": len(vecs[0]) if vecs and vecs[0] else 0,
           "vectors": vecs}
    if _STATE.get("_errors"):
        out["errors"] = _STATE.pop("_errors")
    return out


def embed_texts(texts):
    import torch

    _load()
    model, tok, device = _STATE["model"], _STATE["tokenizer"], _STATE["device"]
    with torch.no_grad():
        t = tok(list(texts)).to(device)
        v = model.encode_text(t)
        v = v / v.norm(dim=-1, keepdim=True)
    vecs = [[float(x) for x in row.detach().cpu().tolist()] for row in v]
    return {"model": _STATE["tag"], "dim": len(vecs[0]) if vecs else 0, "vectors": vecs}


def main():
    if len(sys.argv) < 2:
        _out({"error": "usage: probe | embed-image <paths...> | embed-text <texts...>"})
        return
    cmd = sys.argv[1]
    try:
        if cmd == "probe":
            _out(probe())
        elif cmd == "embed-image":
            paths = sys.argv[2:]
            if not paths:
                _out({"error": "no image paths"})
                return
            _out(embed_images(paths))
        elif cmd == "embed-text":
            texts = sys.argv[2:]
            if not texts:
                _out({"error": "no texts"})
                return
            _out(embed_texts(texts))
        else:
            _out({"error": "unknown cmd: " + cmd})
    except Exception as e:
        _out({"error": "%s: %s" % (type(e).__name__, e)})


if __name__ == "__main__":
    main()
