"""通用型逐帧去字幕精修：残留判据 + 从源帧的候选重做。

设计原则（通用型，不含任何视频特定帧号/坐标/固定亮度阈值）：
- 字幕位置来自 regions.json 的 role=="dialogue" 框（由调用方传入），不写死坐标。
- 字形先验 G 从**源帧自身**用 OCR 文本行 + 行内自适应阈值提取；
  判据的零假设来自**同一帧、同一字幕带内的非字形环带 Ring**，因此天然
  适应亮军装 / 暗光衣服 / 白衬衫 / 亮下巴等不同场景。
- 残留判据以"字形化暗连通块"为准（字尺度几何 + 同帧环带亮度零分布），
  而不是绝对亮度或纯 z 分数：白字变黑字会形成字形块，亮军装纹理/白衬衫
  平补丁则不会。这是实测唯一能同时避开 f0/f1089/f1148 误报的判据。
- 候选相对决策 + 防回退 guard：候选必须实质减少暗字形块，且不得新增亮块、
  平色或结构不连续，才替换当前结果。

本模块为纯函数集合（除可选 OCR），由 dehardsub_ocr_telea 在 opt-in 开关下
调用。import 本模块不应有任何副作用。
"""
from __future__ import annotations

import os
from typing import Any, Iterable

import cv2
import numpy as np


REFINE_ENV = "VITUAL_DEHARDSUB_REFINE"


def refine_enabled() -> bool:
    """Opt-in 开关，风格与 VITUAL_DEHARDSUB_FILL_V2 一致。"""
    return os.environ.get(REFINE_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def _gray(img: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img


# ---------------------------------------------------------------- 字形先验

def _text_regions(src: np.ndarray, box: dict[str, int],
                  text_boxes: Iterable[dict[str, int]] | None = None
                  ) -> list[tuple[int, int, int, int]]:
    """用项目 OCR（或调用方已算好的框）定位字幕行；失败时回退整个字幕框。"""
    regions: list[tuple[int, int, int, int]] = []
    try:
        if text_boxes is not None:
            boxes = list(text_boxes)
        else:
            from glyph_detect import detect_text_boxes
            boxes = detect_text_boxes(src) or []
        bx, by, bw, bh = int(box["x"]), int(box["y"]), int(box["w"]), int(box["h"])
        for b in boxes:
            x, y = int(b.get("x", 0)), int(b.get("y", 0))
            w, h = int(b.get("w", 0)), int(b.get("h", 0))
            # 只取与字幕框重叠且在下半屏的行
            if y + h < by or y > by + bh or w < 20:
                continue
            lx, ly = max(bx, x) - bx, max(by, y) - by
            rx, ry = min(bx + bw, x + w) - bx, min(by + bh, y + h) - by
            if rx > lx and ry > ly:
                regions.append((lx, ly, rx - lx, ry - ly))
    except Exception:
        pass
    if not regions:
        w, h = int(box["w"]), int(box["h"])
        regions = [(0, 0, w, h)]
    return regions


def glyph_prior(src: np.ndarray, box: dict[str, int],
                text_boxes: Iterable[dict[str, int]] | None = None
                ) -> tuple[np.ndarray, np.ndarray]:
    """从源帧提取字形掩码 G 与环带 Ring（局部坐标，尺寸同 box）。

    G: OCR 文本行内用自适应阈值抓亮字笔画（含浅色/描边字），不写死亮度。
    Ring: 紧贴字形但不含字形、且位于字幕行内的背景像素，作为同帧零分布。
    返回 (G: uint8 0/255, Ring: bool)。无字时 G 全 0，调用方应跳过。
    """
    x, y, w, h = int(box["x"]), int(box["y"]), int(box["w"]), int(box["h"])
    H, W = src.shape[:2]
    xa, ya = max(0, x), max(0, y)
    xb, yb = min(W, x + w), min(H, y + h)
    if xb <= xa or yb <= ya:
        return np.zeros((0, 0), np.uint8), np.zeros((0, 0), bool)
    roi = src[ya:yb, xa:xb]
    g = _gray(roi)
    G = np.zeros(g.shape, np.uint8)

    for (lx, ly, lw, lh) in _text_regions(src, box, text_boxes):
        lx, ly = max(0, lx), max(0, ly)
        rx, ry = min(g.shape[1], lx + lw), min(g.shape[0], ly + lh)
        if rx <= lx or ry <= ly:
            continue
        line = g[ly:ry, lx:rx]
        # 自适应阈值：Otsu 抓亮字；低对比帧用较宽的亮度裕度
        thr, _ = cv2.threshold(line, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        lo = max(60.0, min(float(thr), float(np.median(line)) + 35.0))
        ink = (line >= lo).astype(np.uint8) * 255
        # 去噪 + 纳入描边（白字常带深色描边）
        ink = cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
        ink = cv2.dilate(ink, np.ones((3, 3), np.uint8), iterations=1)
        G[ly:ry, lx:rx] = np.maximum(G[ly:ry, lx:rx], ink)

    if not np.any(G):
        return G, np.zeros(g.shape, bool)
    # Ring：字形外扩 3~9px 的环带，排除字形本身
    dil_near = cv2.dilate(G, np.ones((5, 5), np.uint8)) > 0
    dil_far = cv2.dilate(G, np.ones((19, 19), np.uint8)) > 0
    ring = dil_far & ~dil_near
    return G, ring


# ---------------------------------------------------------------- 残影评分

def _multiscale_topbottom_hat(g: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """多尺度白帽(亮细结构)/黑帽(暗细结构)，除以局部背景→光照不变。"""
    L = g.astype(np.float32)
    bg = cv2.GaussianBlur(L, (0, 0), 15) + 1e-6
    wh = np.zeros_like(L); bh = np.zeros_like(L)
    for s in (2, 4, 7, 11, 17):
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * s + 1, 2 * s + 1))
        wh = np.maximum(wh, cv2.morphologyEx(g, cv2.MORPH_TOPHAT, k).astype(np.float32) / s)
        bh = np.maximum(bh, cv2.morphologyEx(g, cv2.MORPH_BLACKHAT, k).astype(np.float32) / s)
    return wh / bg, bh / bg


def _grad_energy(g: np.ndarray, mask: np.ndarray) -> float:
    gx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)
    mag = np.hypot(gx, gy)
    vals = mag[mask]
    return float(np.percentile(vals, 60)) if vals.size else 0.0


def score_result(src_roi: np.ndarray, result_roi: np.ndarray,
                 G: np.ndarray, ring: np.ndarray) -> dict[str, float]:
    """评估结果 ROI 的残留程度。数值越低越好（0 = 干净）。

    返回四项分数：white（白字残留）、black（黑字/黑块）、
    flat（平色补丁）、disc（与同帧环带的结构不连续）。
    """
    out = {"white": 0.0, "black": 0.0, "flat": 0.0, "disc": 0.0}
    glyph = G > 0
    if not glyph.any() or ring.sum() < 32:
        return out
    gs, gr = _gray(src_roi), _gray(result_roi)
    wh, bh = _multiscale_topbottom_hat(gr)

    def zscore(feature: np.ndarray) -> float:
        null = feature[ring]
        if null.size < 16:
            return 0.0
        z = (float(feature[glyph].mean()) - float(null.mean())) / (float(null.std()) + 1e-6)
        return max(0.0, z)

    out["white"] = zscore(wh)
    out["black"] = zscore(bh)

    # 平色补丁：源帧非字形区真实纹理 vs 结果字形区纹理
    ref_tex = _grad_energy(gs, ring)
    cur_tex = _grad_energy(gr, glyph)
    ring_tex = _grad_energy(gr, ring)
    if ref_tex > 1e-6:
        out["flat"] = max(0.0, (ref_tex - cur_tex) / ref_tex)
    if ring_tex > 1e-6:
        out["disc"] = min(2.0, abs(cur_tex - ring_tex) / ring_tex)
    return {k: round(v, 3) for k, v in out.items()}


def cost(sc: dict[str, float]) -> float:
    """综合代价（越低越好）。黑字/平色权重高（最刺眼且最常见误选源）。"""
    return (2.0 * sc.get("black", 0.0)
            + 1.5 * sc.get("flat", 0.0)
            + 1.2 * sc.get("disc", 0.0)
            + 1.0 * sc.get("white", 0.0))


def _character_like_components(mask: np.ndarray) -> list[tuple[int, int, int, int, int]]:
    """统计字形尺度的连通块（通用文字几何：笔画团 4~400px、字高<=45px、宽高比 0.25~5）。"""
    n, _lab, st, _cen = cv2.connectedComponentsWithStats(mask.astype(np.uint8), 8)
    out: list[tuple[int, int, int, int, int]] = []
    for i in range(1, n):
        xx, yy, ww, hh, area = (int(v) for v in st[i])
        if 4 <= area <= 400 and ww <= 60 and hh <= 45 and 0.25 <= ww / max(hh, 1) <= 5.0:
            out.append((xx, yy, ww, hh, area))
    return out


def residual_stats(result_roi: np.ndarray, G: np.ndarray, ring: np.ndarray) -> dict[str, int]:
    """清理结果中的「字形化暗残留」与「新增亮块」数量。

    零分布取自同一帧字幕行内的非字形环带，因此不依赖绝对亮度；只统计落在
    OCR 字形先验 G 内、形态像字的连通块。亮军装/白衬衫上的正常纹理不会形成
    这种连通块，避免 f0/f1148 式误报。
    """
    if ring.sum() < 32 or int((G > 0).sum()) < 200:
        return {"dark_cc": 0, "bright_cc": 0, "dlum": 0.0}
    g = _gray(result_roi)
    med = float(np.median(g[ring]))
    std = float(g[ring].std()) + 1e-6
    glyph = G > 0
    return {
        "dark_cc": len(_character_like_components((g < med - 1.5 * std) & glyph)),
        "bright_cc": len(_character_like_components((g > med + 2.0 * std) & glyph)),
        "dlum": round(float(g[glyph].mean()) - med, 2),
    }


def needs_rework(sc: dict[str, float], dark_cc: int | None = None) -> bool:
    """是否需要重做。有 CC 证据时以「字形化暗残留 >= 2 块」为准；否则用旧版分数兜底。"""
    if dark_cc is not None:
        return dark_cc >= 2
    return (sc.get("black", 0) > 3.0 or sc.get("white", 0) > 3.0
            or sc.get("flat", 0) > 0.35 or sc.get("disc", 0) > 0.6)


# ---------------------------------------------------------------- 候选生成

def candidate_masks(src_roi: np.ndarray, G: np.ndarray) -> dict[str, np.ndarray]:
    """从源帧生成候选 mask（局部坐标），供多种填充方式使用。"""
    g = _gray(src_roi)
    cands: dict[str, np.ndarray] = {}
    # 字形先验本体（最直接：只擦检测到的字）
    if np.any(G):
        cands["glyph"] = G.copy()
    # 多尺度 top-hat 阈值（自适应，抓全字含浅色字）
    acc = np.zeros_like(g, np.float32)
    for s in (2, 4, 7, 11):
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2*s+1, 2*s+1))
        acc = np.maximum(acc, (g.astype(np.float32) -
                               cv2.morphologyEx(g, cv2.MORPH_OPEN, k).astype(np.float32)) / s)
    thr, _ = cv2.threshold(acc.astype(np.uint8), 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    for pad in (0, 2):
        m = (acc >= max(thr, 3.0)).astype(np.uint8) * 255
        if pad:
            m = cv2.dilate(m, np.ones((2*pad+1, 2*pad+1), np.uint8))
        cands[f"tophat_p{pad}"] = m
    return cands


def inpaint_candidates(src_roi: np.ndarray, mask: np.ndarray) -> dict[str, np.ndarray]:
    """同一 mask 的多种填充候选。"""
    out: dict[str, np.ndarray] = {}
    out["telea"] = cv2.inpaint(src_roi, mask, 3, cv2.INPAINT_TELEA)
    out["ns"] = cv2.inpaint(src_roi, mask, 3, cv2.INPAINT_NS)
    try:
        from inpaint_lama import lama_inpaint
        # lama_inpaint 接受整帧；此处 roi 即整帧的子区，直接传入
        out["lama"] = lama_inpaint(src_roi, mask)
    except Exception:
        pass
    return out


def refine_roi(src_roi: np.ndarray, current_roi: np.ndarray,
               box: dict[str, int],
               text_boxes: Iterable[dict[str, int]] | None = None
               ) -> tuple[np.ndarray, dict[str, Any]]:
    """通用逐帧精修入口：评估当前结果，必要时从源帧重做并选优。

    src_roi / current_roi：同尺寸同位置的子图（字幕框 ROI）。
    text_boxes：调用方已有的 OCR 框（与 box 同坐标系，通常由全帧坐标换算到 ROI）；不传则内部再跑一次 OCR。
    返回 (输出 ROI, info)。若当前已干净或候选无改善，原样返回 current_roi。
    """
    info: dict[str, Any] = {"changed": False, "reason": "clean"}
    G, ring = glyph_prior(src_roi, {"x": 0, "y": 0, "w": src_roi.shape[1], "h": src_roi.shape[0]},
                          text_boxes)
    if not np.any(G) or ring.sum() < 32:
        info["reason"] = "no_glyph"
        return current_roi, info

    sc_cur = score_result(src_roi, current_roi, G, ring)
    st_cur = residual_stats(current_roi, G, ring)
    info["current"] = sc_cur
    info["current_cc"] = st_cur
    if not needs_rework(sc_cur, st_cur["dark_cc"]):
        return current_roi, info

    best = current_roi
    best_cost = cost(sc_cur)
    best_reason = "no_better_candidate"
    for mn, mask in candidate_masks(src_roi, G).items():
        if not np.any(mask):
            continue
        # 大面积重画会抹掉制服纹理/画出亮糊斑：只接受小面积修补
        if int((mask > 0).sum()) > 0.40 * G.size:
            continue
        for meth, res in inpaint_candidates(src_roi, mask).items():
            sc = score_result(src_roi, res, G, ring)
            st = residual_stats(res, G, ring)
            c = cost(sc)
            # 硬门槛：暗字形残留必须实质减少，且不得新增亮块/整体亮度漂移/平色
            if st["dark_cc"] > max(1, st_cur["dark_cc"] // 2):
                continue
            if (st["bright_cc"] > st_cur["bright_cc"] + 2
                    or abs(st["dlum"] - st_cur["dlum"]) > 10.0
                    or sc.get("flat", 0) > sc_cur.get("flat", 0) + 0.15
                    or sc.get("black", 0) > sc_cur.get("black", 0) + 0.5
                    or sc.get("disc", 0) > sc_cur.get("disc", 0) + 0.3):
                continue
            if c < best_cost - 0.2:
                best, best_cost, best_reason = res, c, f"{mn}+{meth}"
    info["best"] = best_reason
    info["best_cost"] = round(best_cost, 3)
    if best is not current_roi:
        info["changed"] = True
        info["reason"] = "reworked"
    return best, info
