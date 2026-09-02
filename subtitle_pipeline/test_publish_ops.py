"""WF-09 account slots and WF-08 local_stage publish ledger."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from publish_accounts import (
    PUBLISH_PLATFORMS,
    PublishAccountError,
    bind_account,
    list_slots,
    unbind_account,
    valid_bound,
)
from publish_ops import PublishError, build_caption, run_publish


def _store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "publish_accounts.json"
    monkeypatch.setenv("VITUAL_PUBLISH_ACCOUNTS", str(path))
    return path


def test_slots_start_unbound(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _store(monkeypatch, tmp_path)
    slots = list_slots()
    assert [s["platform"] for s in slots] == list(PUBLISH_PLATFORMS)
    assert all(s["status"] == "unbound" for s in slots)
    assert all("secret" not in s for s in slots)


def test_bind_two_platforms_hides_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = _store(monkeypatch, tmp_path)
    bind_account("douyin", "sessionid=" + ("x" * 16), label="dy", account_id="u1")
    netscape = (
        "# Netscape HTTP Cookie File\n"
        ".kuaishou.com\tTRUE\t/\tFALSE\t1999999999\tsid\tkwsecretvalue99\n"
    )
    bind_account("kuaishou", netscape, label="ks")
    slots = list_slots()
    assert {s["platform"]: s["status"] for s in slots} == {
        "douyin": "valid",
        "kuaishou": "valid",
    }
    blob = json.dumps(slots)
    assert "sessionid" not in blob
    assert "kwsecretvalue99" not in blob
    on_disk = store.read_text(encoding="utf-8")
    assert "sessionid" in on_disk
    assert "kwsecretvalue99" in on_disk
    assert len(valid_bound()) == 2


def test_bind_rejects_unknown_and_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _store(monkeypatch, tmp_path)
    with pytest.raises(PublishAccountError):
        bind_account("youtube", "sessionid=abcdefghijkl")
    with pytest.raises(PublishAccountError):
        bind_account("douyin", "   ")
    bind_account("douyin", "sessionid=" + ("x" * 16))
    unbind_account("douyin")
    assert list_slots()[0]["status"] == "unbound"


def test_netscape_wrong_domain_is_invalid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _store(monkeypatch, tmp_path)
    netscape = (
        "# Netscape HTTP Cookie File\n"
        ".youtube.com\tTRUE\t/\tFALSE\t1999999999\tsid\tabc123456789\n"
    )
    rec = bind_account("douyin", netscape)
    assert rec["status"] == "invalid"


def test_publish_skips_unbound_and_writes_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _store(monkeypatch, tmp_path)
    bind_account("douyin", "sessionid=" + ("x" * 16), account_id="u1")
    media = tmp_path / "media"
    media.mkdir()
    remix = media / "remix"
    remix.mkdir()
    (remix / "remix.mp4").write_bytes(b"\x00" * 1200)
    notes = tmp_path / "notes" / "zh"
    notes.mkdir(parents=True)
    (notes / "summary.json").write_text(
        json.dumps(
            {
                "title": "蒸米饭",
                "one_liner": "温水浸泡两遍。",
                "key_points": [{"title": "温水"}, {"title": "淘洗"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    cap = build_caption(tmp_path)
    assert "温水浸泡" in cap["title"] or cap["title"] == "蒸米饭"
    assert "温水" in cap["body"]

    out = run_publish(tmp_path, platforms=["douyin", "kuaishou"])
    report = json.loads((media / "publish" / "report.json").read_text(encoding="utf-8"))
    by_plat = {row["platform"]: row for row in report["items"]}
    assert by_plat["douyin"]["status"] == "ok"
    assert by_plat["douyin"]["account_id"] == "u1"
    assert by_plat["douyin"]["mode"] == "local_stage"
    assert by_plat["kuaishou"]["status"] == "skipped"
    assert (media / "publish" / "douyin" / "video.mp4").is_file()
    assert not (media / "publish" / "kuaishou" / "video.mp4").exists()
    assert out["ok"] is False  # mixed: one skipped requested platform


def test_publish_no_accounts_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _store(monkeypatch, tmp_path)
    media = tmp_path / "media"
    media.mkdir()
    (media / "source.mp4").write_bytes(b"\x00" * 1200)
    with pytest.raises(PublishError):
        run_publish(tmp_path)


def test_publish_retry_one_platform(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _store(monkeypatch, tmp_path)
    bind_account("douyin", "sessionid=" + ("x" * 16), account_id="u1")
    bind_account(
        "kuaishou",
        "# Netscape\n.kuaishou.com\tTRUE\t/\tFALSE\t1999999999\tsid\tkwsecretvalue99\n",
        account_id="k1",
    )
    media = tmp_path / "media"
    media.mkdir()
    remix = media / "remix"
    remix.mkdir()
    (remix / "remix.mp4").write_bytes(b"\x00" * 1200)
    run_publish(tmp_path, platforms=["douyin"])
    run_publish(tmp_path, retry_platform="kuaishou")
    report = json.loads((media / "publish" / "report.json").read_text(encoding="utf-8"))
    plats = {row["platform"] for row in report["items"]}
    assert plats == {"douyin", "kuaishou"}
    assert all(row["status"] == "ok" for row in report["items"])


def test_parse_stages_publish() -> None:
    from discover.run_batch import parse_stages
    from api.try_service import compose_try_stages

    assert parse_stages("publish") == frozenset({"publish"})
    assert "publish" not in parse_stages("all")
    mixed = parse_stages("all,publish")
    assert "asr" in mixed and "publish" in mixed
    assert compose_try_stages(
        want_translate=True, want_notes=True, want_publish=True
    ) == "all,publish"
