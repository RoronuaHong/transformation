# -*- coding: utf-8 -*-
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from visual_runner import _parse


def test_parse_keeps_last_real_caption():
    text = "描述: 请提供图片\n描述: 一盘红烧鸡翅\n标签: 鸡翅,芝麻\nEN描述: chicken wings"
    description, tags, en_description, _en_tags = _parse(text)
    assert description == "一盘红烧鸡翅"
    assert "鸡翅" in tags
    assert en_description == "chicken wings"


def test_parse_drops_refusal_only():
    description, tags, _en, _ent = _parse("描述: 无法观察到图片，请提供需要描述的图片。")
    assert description == ""
    assert tags == ""
