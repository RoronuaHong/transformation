"""开放标签通道(打标开放化)单测:_open_entities 从 VLM 描述抽 tag_vocab 之外的具体实体。

SigLIP2×tag_vocab 仍是零幻觉主力(verified/auto);open 是低权重补充,归一层(停用词/泛化
黑名单/长度/去重/去 tag_vocab 已覆盖)控噪。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import core  # noqa: E402


def test_open_entities_extracts_novel():
    vocab_words = {"chicken", "wings", "kitchen", "cooking", "person"}
    vlm = "A person cooking chicken wings in a modern kitchen with a wok"
    ens = [o["en"] for o in core._open_entities(vlm, vocab_words, limit=8)]
    assert "wok" in ens, ens
    assert "modern" in ens, ens
    # tag_vocab 已覆盖的不进 open(去重,不与 SigLIP2 通道重复)
    assert not any(e in ("chicken", "wings", "kitchen", "person") for e in ens), ens
    assert "chicken wings" not in ens, ens


def test_open_entities_empty():
    assert core._open_entities("", {"x"}) == []
    assert core._open_entities("a photo of a scene", set()) == []


def test_open_entities_dedup():
    ens = [o["en"] for o in core._open_entities("wok wok wok pan", set(), limit=8)]
    assert ens.count("wok") == 1


def test_open_entities_limit():
    vlm = "alpha beta gamma delta epsilon zeta eta theta"
    assert len(core._open_entities(vlm, set(), limit=3)) == 3


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
