"""Stage presets, try langs, and resubmit intent."""

from __future__ import annotations

from api.try_service import (
    langs_equal,
    normalize_try_langs,
    resolve_try_intent,
)
from discover.run_batch import parse_stages, resolve_media_refresh_stages
from langs import PACKS, expand_pack_codes, resolve_targets
from pipeline import DEFAULT_LANGS, notes_minimally_valid


def test_parse_stages_presets() -> None:
    assert "asr" in parse_stages("all")
    assert parse_stages("clips") == frozenset({"clips"})
    assert parse_stages("media") == frozenset({"frames", "clips"})
    assert parse_stages("llm") == frozenset({"translate", "notes", "localize"})
    assert "asr" not in parse_stages("llm")
    assert parse_stages("post") == frozenset(
        {"translate", "notes", "localize", "frames", "clips"}
    )
    assert parse_stages("translate,localize") == frozenset({"translate", "localize"})


def test_resolve_media_refresh_stages() -> None:
    assert resolve_media_refresh_stages(
        stages=None, frames="none", has_clips=True
    ) == frozenset({"clips"})
    assert resolve_media_refresh_stages(
        stages="media", frames="auto", has_clips=True
    ) == frozenset({"frames", "clips"})
    assert resolve_media_refresh_stages(
        stages="enhance", frames="auto", has_clips=False
    ) == frozenset({"enhance"})


def test_normalize_try_langs() -> None:
    assert normalize_try_langs(None) == "site"
    assert normalize_try_langs([]) == "site"
    assert normalize_try_langs(list(PACKS["site"])) == "site"
    assert normalize_try_langs(["en", "ja", "JA"]) == "en,ja"
    assert normalize_try_langs("en,zh-Hant") == "en,zh-Hant"
    assert normalize_try_langs("all") == "site"
    assert langs_equal("site", list(PACKS["site"]))
    assert langs_equal("en,ja", ["ja", "en"])
    assert not langs_equal("en,ja", "en,ko")


def test_default_langs_and_site_pack_sync() -> None:
    assert DEFAULT_LANGS == "site"
    site = set(PACKS["site"])
    ui = {
        "zh",
        "en",
        "ru",
        "ja",
        "ko",
        "pt",
        "de",
        "zh-Hant",
        "es",
        "fr",
        "ar",
        "hi",
        "id",
        "vi",
        "th",
        "tr",
    }
    assert site == ui
    assert len(expand_pack_codes("site")) == 16
    assert len(resolve_targets("site", "zh")) == 15
    assert "zh" not in resolve_targets("site", "zh")
    assert resolve_targets("en,ja", "en") == ["ja"]


def test_compose_try_stages() -> None:
    from api.try_service import compose_try_stages

    assert compose_try_stages(want_translate=True, want_notes=True) == "all"
    assert "translate" in compose_try_stages(
        want_translate=True, want_notes=False, has_media=False
    )
    assert "notes" in compose_try_stages(
        want_translate=False, want_notes=True, has_media=True
    )
    assert "clips" in compose_try_stages(
        want_translate=False, want_notes=False, has_media=True
    )


def test_resolve_try_intent() -> None:
    assert (
        resolve_try_intent(job_status=None, new_langs="site")["intent"] == "full"
    )
    # explicit stages wins over langs change
    assert (
        resolve_try_intent(
            job_status="done",
            stages="clips",
            frames="none",
            has_clips=True,
            new_langs="en",
            prev_langs="site",
        )["intent"]
        == "clips"
    )
    # langs change → post
    assert (
        resolve_try_intent(
            job_status="done",
            frames="auto",
            new_langs="en,ja",
            prev_langs="site",
            new_frame_opts={"frames": "auto", "gif_sec": 4, "clips": [], "gif_ranges": []},
            prev_frame_opts={"frames": "auto", "gif_sec": 4, "clips": [], "gif_ranges": []},
        )["intent"]
        == "post"
    )
    same = {"frames": "auto", "gif_sec": 4.0, "clips": [], "gif_ranges": []}
    assert (
        resolve_try_intent(
            job_status="done",
            frames="auto",
            new_langs="site",
            prev_langs="site",
            new_frame_opts=same,
            prev_frame_opts=same,
        )["intent"]
        == "noop"
    )
    # same langs, clips only
    assert (
        resolve_try_intent(
            job_status="done",
            frames="none",
            has_clips=True,
            new_langs="site",
            prev_langs="site",
            new_frame_opts={
                "frames": "none",
                "gif_sec": 4,
                "clips": [{"start": 1, "end": 3}],
                "gif_ranges": [],
            },
            prev_frame_opts=same,
        )["intent"]
        == "clips"
    )
    # frame mode change without ranges → media
    assert (
        resolve_try_intent(
            job_status="done",
            frames="gif",
            new_langs="site",
            prev_langs="site",
            new_frame_opts={"frames": "gif", "gif_sec": 4, "clips": [], "gif_ranges": []},
            prev_frame_opts=same,
        )["intent"]
        == "frames"
    )
    # finished job + enhance → media postproc, not noop
    assert (
        resolve_try_intent(
            job_status="done",
            stages="all,enhance",
            frames="auto",
            new_langs="site",
            prev_langs="site",
            new_frame_opts=same,
            prev_frame_opts=same,
        )["stages"]
        == "enhance"
    )
