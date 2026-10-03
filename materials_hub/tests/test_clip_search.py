# -*- coding: utf-8 -*-
"""CLIP / image_embeddings 离线测试(mock runner,不下载权重)。

运行: python tests/test_clip_search.py
"""
import os
import sys
import tempfile
import shutil

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core  # noqa: E402


def _setup():
    tmp = tempfile.mkdtemp(prefix="hub_clip_", dir=core.HUB)
    core.INDEX_DIR = tmp
    core.INDEX_DB = os.path.join(tmp, "hub.db")
    core.MATERIALS = os.path.join(tmp, "materials")
    core.THUMBS = os.path.join(tmp, "thumbs")
    core.PHASH_DIR = os.path.join(tmp, "phash")
    core.OCR_DIR = os.path.join(tmp, "ocr")
    core.SHOT_DIR = os.path.join(tmp, "shots")
    for d in (core.MATERIALS, core.THUMBS, core.PHASH_DIR, core.OCR_DIR, core.SHOT_DIR):
        os.makedirs(d, exist_ok=True)
    for k in ("videos", "silent", "images"):
        os.makedirs(os.path.join(core.MATERIALS, k), exist_ok=True)
    core._init_db()
    core._CLIP_PROBE_CACHE.update(probed=False, info=None)
    return tmp


def _add(mid, kind="images", name="a.jpg"):
    p = os.path.join(core.MATERIALS, kind, name)
    with open(p, "wb") as f:
        f.write(b"\x00" * 32)
    core.add_material(
        id=mid, kind=kind, ext=os.path.splitext(name)[1], name=name,
        rel_path=os.path.relpath(p, core.HUB), size=32, sha256=mid + "0" * 20,
        tags="", description="", source="t", orig_name=name,
        created_at="2026-09-30T00:00:00",
    )
    # 假封面供 _clip_visual_path
    with open(core.thumb_path(mid), "wb") as f:
        f.write(b"jpg")
    return mid


def test_build_and_search_clip_mocked():
    tmp = _setup()
    orig_probe = core.clip_probe
    orig_embed_img = core._clip_embed_images
    orig_embed_txt = core._clip_embed_texts
    try:
        _add("m1aaaaaaa001", "images", "cat.jpg")
        _add("m2bbbbbbb002", "images", "dog.jpg")
        # 正交近似向量
        v_cat = [1.0, 0.0, 0.0, 0.0]
        v_dog = [0.0, 1.0, 0.0, 0.0]
        core.clip_probe = lambda refresh=False: {
            "ok": True, "backend": "local", "model": "mock/clip", "dim": 4,
            "url": "", "err": "",
        }
        mapping = {}

        def fake_imgs(paths):
            out = []
            for p in paths:
                if "cat" in p or p.endswith("m1aaaaaaa001.jpg") or "thumbs" in p.replace("\\", "/"):
                    # thumb path uses mid
                    mid = os.path.splitext(os.path.basename(p))[0]
                    out.append(v_cat if mid.startswith("m1") else v_dog)
                else:
                    mid = os.path.splitext(os.path.basename(p))[0]
                    out.append(v_cat if mid.startswith("m1") else v_dog)
            return out

        core._clip_embed_images = fake_imgs
        core._clip_embed_texts = lambda texts: [
            v_cat if "cat" in t.lower() or "猫" in t else v_dog for t in texts
        ]

        r = core.build_image_embeddings()
        assert r["available"] and r["embedded"] == 2, r
        st = core.image_embed_status()
        assert st["embedded"] == 2, st

        # 以文搜图
        hit = core.search_by_text_image("a cat", limit=5, min_score=0.1)
        assert hit["status"] == "ok", hit
        assert hit["matches"][0]["id"].startswith("m1"), hit

        # 以图搜图 clip
        hit2 = core.search_by_image("m1aaaaaaa001", mode="clip", limit=5)
        assert hit2["status"] == "ok" and hit2["mode"] == "clip", hit2
        # 不应含自身,第二应是 dog 或空
        ids = [m["id"] for m in hit2["matches"]]
        assert "m1aaaaaaa001" not in ids, ids
        print("PASS test_build_and_search_clip_mocked")
    finally:
        core.clip_probe = orig_probe
        core._clip_embed_images = orig_embed_img
        core._clip_embed_texts = orig_embed_txt
        core._CLIP_PROBE_CACHE.update(probed=False, info=None)
        shutil.rmtree(tmp, ignore_errors=True)


def test_clip_unavailable_falls_back_phash():
    tmp = _setup()
    orig = core.clip_probe
    try:
        _add("m3ccccccc003")
        core.PHASH_DIR = os.path.join(tmp, "phash")
        os.makedirs(core.PHASH_DIR, exist_ok=True)
        with open(core.phash_path("m3ccccccc003"), "w", encoding="utf-8") as f:
            f.write("ffffffffffffffff")
        core.clip_probe = lambda refresh=False: {
            "ok": False, "backend": "", "err": "no_weights", "url": "",
        }
        r = core.search_by_image("m3ccccccc003", mode="auto")
        assert r["status"] == "ok" and r["mode"] == "phash", r
        r2 = core.search_by_image("m3ccccccc003", mode="clip")
        assert r2["status"] == "unavailable", r2
        print("PASS test_clip_unavailable_falls_back_phash")
    finally:
        core.clip_probe = orig
        core._CLIP_PROBE_CACHE.update(probed=False, info=None)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as e:
                fails += 1
                print("FAIL %s: %s: %s" % (name, type(e).__name__, e))
    if fails:
        sys.exit(1)
    print("ALL PASS")
