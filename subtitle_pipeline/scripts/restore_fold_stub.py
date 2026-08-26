#!/usr/bin/env python3
"""Restore minimal Bilibili folding article so site pages stop 404ing."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from discover.content_db import ContentDB
from discover.export_site import main as export_main
from discover.run_batch import register_work_dir
from langs import resolve_targets

SLUG = "bv1o24y1k7j7"
VIDEO_ID = "BV1o24y1k7j7"
URL = "https://www.bilibili.com/video/BV1o24y1k7j7"

KEY_POINT_ROWS = [
    (
        "T恤叠法（豆腐块）",
        "先将T恤折到领口位置三折，然后对折袖子，再将衣头往内折，使T恤形成一个豆腐块的形状。",
    ),
    (
        "T恤尺寸匹配",
        "确定叠衣服的宽度和高度时，应根据收纳箱的实际宽度和高度来匹配，以确保完美收纳。",
    ),
    (
        "长袖T恤叠法",
        "采用平整折叠，将肩膀和袖子折入，并确保袖子长度小于下摆，形成豆腐块状。",
    ),
    (
        "肌肉卷法（T恤）",
        "通过翻折和双手夹力，将衣头卷入，可以大幅减少体积，适合打包使用。",
    ),
    (
        "卫衣叠法（口袋造型）",
        "利用衣头和袖子折叠，形成一个口袋造型，便于收纳。",
    ),
    (
        "戴帽卫衣叠法",
        "将卫衣折叠后，利用帽子本身进行包裹，使卫衣形成帽子造型，体积最小。",
    ),
    (
        "毛衣口袋法",
        "无需卷边，直接将衣头裁剪到衣尾处，利用三分之一肩膀处折叠形成小口袋，方便收纳。",
    ),
    (
        "大衣折叠法",
        "沿着肩线将大衣折叠至内侧，并折叠下摆，利用下摆的开口形成自然口袋。",
    ),
    (
        "羽绒服叠法（简单版）",
        "将衣尾打开一个小口袋，将衣头塞进去，形成一个小被子状，便于收纳。",
    ),
    (
        "羽绒服叠法（帽子版）",
        "将袖子平折，然后用帽子本身进行翻折收纳，使羽绒服体积缩小且不易滑动。",
    ),
]

KEY_POINTS = [
    {
        "title": title,
        "detail": detail,
        "image": f"/frames/{SLUG}/kp_{i:02d}.gif",
    }
    for i, (title, detail) in enumerate(KEY_POINT_ROWS)
]

FOCUSES = [
    {
        "title": "T恤的豆腐块叠法",
        "detail": "通过三折和内折，将T恤折成一个厚度适中的方块，便于收纳。",
    },
    {
        "title": "利用尺寸匹配收纳箱",
        "detail": "叠衣服的宽度和高度必须与收纳箱的尺寸匹配，以最大化空间利用率。",
    },
    {
        "title": "毛衣和羽绒服的口袋法",
        "detail": "利用衣头或袖子形成口袋，或利用帽子进行包裹，实现更紧凑的收纳。",
    },
    {
        "title": "衬衫的悬挂与折叠",
        "detail": "建议悬挂以减少褶皱，若需折叠，应注意袖子和衣头的位置。",
    },
]

HARD_POINTS = [
    {
        "title": "尺寸匹配是关键",
        "detail": "叠衣服的宽度和高度必须精确匹配收纳箱的尺寸，否则无法最大化空间利用。",
    },
    {
        "title": "避免褶皱",
        "detail": "对于衬衫等易皱衣物，建议悬挂；折叠时要确保袖子和衣头平整。",
    },
    {
        "title": "长袖折叠的比例",
        "detail": "在折叠长袖时，必须确保袖子长度小于下摆，以保证形状的完整性。",
    },
]

TITLE = "超全叠衣服大法：T恤、毛衣、大衣及羽绒服的收纳技巧"
ONE_LINER = "通过特定的折叠方法，可以最大化衣物的体积，实现更紧凑的收纳。"


def main() -> int:
    work = ROOT / "downloads" / "batch" / f"bilibili_{VIDEO_ID}"
    notes_zh = work / "notes" / "zh"
    notes_zh.mkdir(parents=True, exist_ok=True)
    summary = {
        "title": TITLE,
        "source_lang": "zh",
        "summary_lang": "zh",
        "one_liner": ONE_LINER,
        "summary": ONE_LINER,
        "outline": [f["title"] for f in FOCUSES],
        "focuses": FOCUSES,
        "key_points": KEY_POINTS,
        "hard_points": HARD_POINTS,
        "keypoints_intro": ONE_LINER,
    }
    notes_zh.joinpath("summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    content = ContentDB()
    try:
        aid = register_work_dir(
            content,
            platform="bilibili",
            video_id=VIDEO_ID,
            topic_id="home_tips",
            canonical_url=URL,
            work_dir=work,
            source_lang="zh",
            title=TITLE,
            locales=resolve_targets("site", "zh"),
        )
        print(f"[restore] registered article_id={aid}")
    finally:
        content.close()

    export_main([])
    print("[restore] exported site/content/articles.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
