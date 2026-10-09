# -*- coding: utf-8 -*-
"""Pytest 共享夹具:每个测试完全隔离 core 模块状态 + 独立临时素材库 DB,
消除同进程跨测试全局泄漏(ffmpeg_path/_http_json/chat_models/subprocess.run/
INDEX_DB 等)导致的顺序相关失败。仅 pytest 运行时生效,不影响真实运行(agent/cli/mcp)。
"""
import os
import sys
import tempfile
import shutil

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_core():
    saved = dict(vars(core))          # 快照当前模块全部属性
    sub_run = core.subprocess.run     # 模块属性,vars(core) 快照无法覆盖,单独保存
    # 临时库放在 HUB 同盘下:core.health() 用 os.path.relpath(INDEX_DB, HUB),跨盘会抛 ValueError
    tmp = tempfile.mkdtemp(prefix="hub_test_", dir=core.HUB)
    # 重定向全部派生目录到 tmp,隔离 sidecar 文件(OCR/镜头/pHash/封面/素材)的跨测试泄漏
    core.MATERIALS = os.path.join(tmp, "materials")
    core.INGEST = os.path.join(tmp, "ingest")
    core.TRASH = os.path.join(tmp, "trash")
    core.INDEX_DIR = tmp
    core.OCR_DIR = os.path.join(tmp, "ocr")
    core.SHOT_DIR = os.path.join(tmp, "shots")
    core.PHASH_DIR = os.path.join(tmp, "phash")
    core.THUMBS = os.path.join(tmp, "thumbs")
    core.STATIC = os.path.join(tmp, "static")
    # 派生 sidecar/记录目录也要隔离,否则跨测试泄漏(如 attach 写 index/asr 命中上次残留)
    core.ASR_DIR = os.path.join(tmp, "asr")
    core.VISUAL_DIR = os.path.join(tmp, "visual")
    core.RUN_DIR = os.path.join(tmp, "run")
    core.UNDERSTAND_DIR = os.path.join(tmp, "understand")
    core.INDEX_DB = os.path.join(tmp, "hub.db")
    # 反馈日志 / 否决词表也随 INDEX_DIR 隔离(否则跨测试/真实库泄漏,
    # 步骤 10 的 _is_vetoed 会读到真实库的否决导致组装过滤失真)
    core._FEEDBACK_PATH = os.path.join(tmp, ".agent_feedback.jsonl")
    core._VETO_PATH = os.path.join(tmp, ".agent_vetoes.json")
    core._init_db()
    try:
        yield
    finally:
        # 清除本测试新增的属性,并恢复被本测试(或前序泄漏)改动的全部属性
        added = set(vars(core).keys()) - set(saved.keys())
        for k in added:
            delattr(core, k)
        for k, v in saved.items():
            setattr(core, k, v)
        core.subprocess.run = sub_run
        shutil.rmtree(tmp, ignore_errors=True)
