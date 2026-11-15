"""Stage presets, try langs, resubmit intent, and ASR backend guards."""

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
    assert parse_stages("content") == parse_stages("all")
    assert "dehardsub" not in parse_stages("content")
    assert parse_stages("clips") == frozenset({"clips"})
    assert parse_stages("media") == frozenset({"frames", "clips"})
    assert parse_stages("llm") == frozenset({"translate", "notes", "localize"})
    assert "asr" not in parse_stages("llm")
    assert parse_stages("post") == frozenset(
        {"translate", "notes", "localize", "frames", "clips"}
    )
    assert parse_stages("translate,localize") == frozenset({"translate", "localize"})
    assert parse_stages("postproc") == frozenset(
        {"dehardsub", "deblur", "enhance", "compress"}
    )
    mixed = parse_stages("content,postproc")
    assert "asr" in mixed and "dehardsub" in mixed
    from discover.run_batch import describe_tracks

    tracks = describe_tracks(mixed)
    assert "asr" in tracks["content"]
    assert "dehardsub" in tracks["postproc"]


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
    assert normalize_try_langs(None) == "fununion20"
    assert normalize_try_langs([]) == "fununion20"
    assert normalize_try_langs(list(PACKS["fununion20"])) == "fununion20"
    assert normalize_try_langs(["en", "ja", "JA"]) == "en,ja"
    assert normalize_try_langs("en,zh-Hant") == "en,zh-Hant"
    assert normalize_try_langs("all") == "fununion20"
    assert normalize_try_langs("site") == "site"
    assert langs_equal("fununion20", list(PACKS["fununion20"]))
    assert langs_equal("en,ja", ["ja", "en"])
    assert not langs_equal("en,ja", "en,ko")


def test_default_langs_and_site_pack_sync() -> None:
    assert DEFAULT_LANGS == "fununion20"
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
    assert len(expand_pack_codes("fununion20")) == 20
    assert len(resolve_targets("fununion20", "zh")) == 19
    assert "zh" not in resolve_targets("fununion20", "zh")
    assert len(resolve_targets("site", "zh")) == 15
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
    assert (
        resolve_try_intent(
            job_status="done",
            stages="all,remix",
            frames="auto",
            new_langs="site",
            prev_langs="site",
            new_frame_opts=same,
            prev_frame_opts=same,
        )["stages"]
        == "remix"
    )


# ---- ASR backend guard：低内存 Windows 的 ctranslate2 MKL 自愈 ----


def _ct2_guard_env(monkeypatch, *, os_name="nt", ram=3.0, modules=None, preset=None):
    """隔离 ensure_ct2_backend 的环境依赖，返回它写入的 env 副本（不污染真进程）。"""
    from types import SimpleNamespace

    import multipass_asr as mp

    env: dict[str, str] = {}
    if preset is not None:
        env["CT2_USE_MKL"] = preset
    monkeypatch.setattr(mp, "os", SimpleNamespace(name=os_name, environ=env))
    monkeypatch.setattr(mp, "sys", SimpleNamespace(modules=modules or {}))
    monkeypatch.setattr(mp, "_available_ram_gb", lambda: ram)
    return env


def test_ct2_backend_disabled_on_low_ram_windows(monkeypatch):
    from multipass_asr import ensure_ct2_backend

    env = _ct2_guard_env(monkeypatch, ram=3.0)
    ensure_ct2_backend()
    # ctranslate2 4.8.1 只认 CT2_USE_MKL(=0 切 DNNL)；CT2_USE_OPENBLAS/EIGEN
    # 在该版本 DLL 里根本不存在，不能设置无效变量。
    assert env == {
        "CT2_USE_MKL": "0",
        "OMP_NUM_THREADS": "2",
        "MKL_NUM_THREADS": "2",
    }


def test_ct2_backend_respects_explicit_env(monkeypatch):
    from multipass_asr import ensure_ct2_backend

    env = _ct2_guard_env(monkeypatch, ram=3.0, preset="1")
    ensure_ct2_backend()
    assert env == {"CT2_USE_MKL": "1"}


def test_ct2_backend_untouched_on_ample_ram(monkeypatch):
    from multipass_asr import ensure_ct2_backend

    env = _ct2_guard_env(monkeypatch, ram=16.0)
    ensure_ct2_backend()
    assert env == {}


def test_ct2_backend_untouched_when_ct2_already_imported(monkeypatch):
    from multipass_asr import ensure_ct2_backend

    env = _ct2_guard_env(monkeypatch, ram=3.0, modules={"ctranslate2": object()})
    ensure_ct2_backend()
    assert env == {}


def test_ct2_backend_untouched_off_windows(monkeypatch):
    from multipass_asr import ensure_ct2_backend

    env = _ct2_guard_env(monkeypatch, os_name="posix", ram=3.0)
    ensure_ct2_backend()
    assert env == {}


# ---- 内存日志：物理 RAM / 提交量+页面文件 / 显存三种口径必须分开 ----


def test_memory_snapshot_distinguishes_scopes():
    from multipass_asr import _memory_snapshot

    snap = _memory_snapshot()
    if snap is None:  # 非 Windows 或 ctypes 不可用
        return
    assert set(snap) >= {
        "ram_total", "ram_avail", "commit_limit", "commit_avail", "pagefile_size"
    }
    assert 0 <= snap["ram_avail"] <= snap["ram_total"]
    assert 0 <= snap["commit_avail"] <= snap["commit_limit"]
    assert snap["pagefile_size"] >= 0


def _fake_snapshot(avail: float) -> dict:
    return {
        "ram_total": 32.0,
        "ram_avail": avail,
        "commit_limit": 73.4,
        "commit_avail": 6.3,
        "pagefile_size": 41.5,
    }


def test_log_asr_memory_cuda_labels_all_scopes(monkeypatch, capsys):
    import multipass_asr as mp

    monkeypatch.setattr(mp, "_memory_snapshot", lambda: _fake_snapshot(6.4))
    monkeypatch.setattr(mp, "_vram_free_gb", lambda: 1.5)
    mp._log_asr_memory("fake-model", device="cuda")
    out = capsys.readouterr().out
    assert "RAM avail=6.4/32.0GB" in out
    assert "commit avail=6.3/73.4GB" in out
    assert "pagefile=41.5GB" in out
    assert "VRAM free=1.5GB" in out
    assert "WARNING" in out


def test_log_asr_memory_cpu_skips_vram_and_high_ram_no_warn(monkeypatch, capsys):
    import multipass_asr as mp

    monkeypatch.setattr(mp, "_memory_snapshot", lambda: _fake_snapshot(16.0))
    monkeypatch.setattr(mp, "_vram_free_gb", lambda: 1.5)
    mp._log_asr_memory("fake-model", device="cpu")
    out = capsys.readouterr().out
    assert "VRAM" not in out
    assert "WARNING" not in out


def test_vram_free_gb_parses_nvidia_smi(monkeypatch):
    from types import SimpleNamespace

    import multipass_asr as mp

    monkeypatch.setattr(
        mp, "subprocess",
        SimpleNamespace(
            run=lambda *a, **k: SimpleNamespace(
                returncode=0, stdout="1521\n", stderr=""
            )
        ),
    )
    assert abs(mp._vram_free_gb() - 1521 / 1024) < 1e-6
