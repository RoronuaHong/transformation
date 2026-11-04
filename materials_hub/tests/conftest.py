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


# 测试临时目录统一落点:放 index/ 下(派生数据、可随时删),既与 HUB 同盘满足
# core.health() 的 os.path.relpath(INDEX_DB, HUB) 约束,又不让残留散落项目根。
# 历史上各测试各自 mkdtemp(dir=core.HUB),异常/中断时在项目根留下上百个 hub_* 目录。
PYTEST_TMP_ROOT = os.path.join(core.HUB, "index", "_pytest_tmp")


def _hub_tmp_root():
    """返回测试暂存目录(不存在则建);各测试 mkdtemp 统一用它作 dir。"""
    os.makedirs(PYTEST_TMP_ROOT, exist_ok=True)
    return PYTEST_TMP_ROOT


@pytest.fixture(scope="session", autouse=True)
def _purge_pytest_tmp():
    """整轮 pytest 结束清空暂存区,兜底回收忘记清理/异常中断的临时目录。"""
    yield
    shutil.rmtree(PYTEST_TMP_ROOT, ignore_errors=True)


@pytest.fixture(autouse=True)
def _isolate_core():
    saved = dict(vars(core))          # 快照当前模块全部属性
    sub_run = core.subprocess.run     # 模块属性,vars(core) 快照无法覆盖,单独保存
    # 临时库放在 HUB 同盘下:core.health() 用 os.path.relpath(INDEX_DB, HUB),跨盘会抛 ValueError
    tmp = tempfile.mkdtemp(prefix="hub_test_", dir=_hub_tmp_root())
    # 重定向全部派生目录到 tmp,隔离 sidecar 文件(OCR/镜头/pHash/封面/素材)的跨测试泄漏
    core.MATERIALS = os.path.join(tmp, "materials")
    core.INGEST = os.path.join(tmp, "ingest")
    core.TRASH = os.path.join(tmp, "trash")
    core.INDEX_DIR = tmp
    core.OCR_DIR = os.path.join(tmp, "ocr")
    core.SHOT_DIR = os.path.join(tmp, "shots")
    core.PHASH_DIR = os.path.join(tmp, "phash")
    core.TECH_DIR = os.path.join(tmp, "tech")   # 技术元数据 sidecar;漏隔离会写进真实 index/tech(2026-10-15 实测泄漏)
    core.THUMBS = os.path.join(tmp, "thumbs")
    core.STATIC = os.path.join(tmp, "static")
    # 派生 sidecar/记录目录也要隔离,否则跨测试泄漏(如 attach 写 index/asr 命中上次残留)
    core.ASR_DIR = os.path.join(tmp, "asr")
    core.VISUAL_DIR = os.path.join(tmp, "visual")
    core.AUTOTAG_DIR = os.path.join(tmp, "autotags")
    core.RUN_DIR = os.path.join(tmp, "run")
    core.UNDERSTAND_DIR = os.path.join(tmp, "understand")
    core.INDEX_DB = os.path.join(tmp, "hub.db")
    # 这两个是"导入期由 INDEX_DIR 算出"的模块级路径,单改 INDEX_DIR 不会重算,需显式重定向
    core._CACHE_PATH = os.path.join(tmp, "search_cache.pkl")
    core.AUTOTAG_BACKUP = os.path.join(tmp, "autotag_backup.json")
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
