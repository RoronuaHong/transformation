"""Decide whether notes need frames, GIF vs still, and GIF length.

Product (home_tips): show motion only for visual how-tos. Pure talk / science
explainers stay text-only so the page does not fake "steps" with random clips.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

MediaKind = Literal["none", "gif", "jpg"]

# Topics that may attach frames at all.
VISUAL_TOPICS = frozenset({"home_tips"})

# Strong signals the video is a hands-on demo.
ACTION_MARKERS = (
    "收纳",
    "折叠",
    "折叠法",
    "清洁",
    "清洗",
    "擦",
    "步骤",
    "教程",
    "怎么折",
    "怎么叠",
    "整理",
    "厨房",
    "卫生间",
    "衣柜",
    "叠衣",
    "折衣",
    "howto",
    "how to",
    "fold",
    "organiz",
    "clean",
    "scrub",
    "diy",
)

# Signals that frames add little (talk / listicle / theory).
TALK_MARKERS = (
    "科学",
    "原理",
    "为什么",
    "习惯",
    "心态",
    "评测",
    "推荐清单",
    "排行",
    "访谈",
    "故事",
    "科普",
    "理论",
    "减肥方法",  # diet talk unless also action
    "饮食习惯",
)

DEFAULT_GIF_SEC = 4.0
MIN_GIF_SEC = 1.0
MAX_GIF_SEC = 20.0


@dataclass(frozen=True)
class FrameDecision:
    enabled: bool
    media: MediaKind
    gif_duration: float
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "media": self.media,
            "gif_duration": self.gif_duration,
            "reason": self.reason,
        }


def _blob(title: str, key_points: list[Any] | None) -> str:
    parts = [title or ""]
    for kp in key_points or []:
        if isinstance(kp, dict):
            parts.append(str(kp.get("title") or ""))
            parts.append(str(kp.get("detail") or ""))
        else:
            parts.append(str(kp))
    return "\n".join(parts).lower()


def _count_hits(text: str, markers: tuple[str, ...]) -> int:
    return sum(1 for m in markers if m.lower() in text)


def decide_frames(
    *,
    topic_id: str,
    title: str = "",
    key_points: list[Any] | None = None,
    has_video: bool = False,
    force_media: MediaKind | None = None,
    gif_duration: float | None = None,
) -> FrameDecision:
    """Gate frame extraction for a finished notes summary."""
    dur = DEFAULT_GIF_SEC if gif_duration is None else float(gif_duration)
    dur = max(MIN_GIF_SEC, min(MAX_GIF_SEC, dur))

    if force_media == "none":
        return FrameDecision(False, "none", dur, "forced off")
    if not has_video:
        return FrameDecision(False, "none", dur, "no source video")
    if topic_id not in VISUAL_TOPICS:
        return FrameDecision(False, "none", dur, f"topic {topic_id} is text-only")

    text = _blob(title, key_points)
    action_n = _count_hits(text, ACTION_MARKERS)
    talk_n = _count_hits(text, TALK_MARKERS)

    if force_media in ("gif", "jpg"):
        return FrameDecision(True, force_media, dur, f"forced {force_media}")

    # Hands-on demo → short GIF loops (default 4s).
    if action_n >= 1 and action_n >= talk_n:
        return FrameDecision(
            True,
            "gif",
            dur,
            f"visual how-to (action={action_n}, talk={talk_n}), gif={dur:.0f}s",
        )

    # Mostly talk / science → no decorative clips.
    if talk_n > action_n:
        return FrameDecision(
            False,
            "none",
            dur,
            f"talk/explainer (talk={talk_n}, action={action_n})",
        )

    # Ambiguous home_tips with video: still prefer a single still over GIF noise.
    if action_n == 0 and talk_n == 0:
        return FrameDecision(
            True,
            "jpg",
            dur,
            "home_tips + video, no clear action/talk markers → still only",
        )

    return FrameDecision(
        True,
        "gif",
        dur,
        f"home_tips default gif={dur:.0f}s (action={action_n}, talk={talk_n})",
    )
