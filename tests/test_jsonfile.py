"""Data files: what survives a power cut or a damaged file."""
from __future__ import annotations

import json

import pytest

from promin.jsonfile import read_json, write_json

from helpers import add_fake_video, make_client, video_ids


def test_write_json_keeps_the_previous_version(tmp_path):
    path = tmp_path / "data.json"
    write_json(path, {"v": 1})
    write_json(path, {"v": 2}, private=True)
    assert json.loads(path.read_text()) == {"v": 2}
    assert json.loads((tmp_path / "data.json.bak").read_text()) == {"v": 1}
    assert path.stat().st_mode & 0o777 == 0o600
    assert not (tmp_path / "data.json.tmp").exists()


def test_damaged_file_falls_back_to_the_backup_and_is_kept(tmp_path):
    path = tmp_path / "data.json"
    write_json(path, {"v": 1})
    write_json(path, {"v": 2})
    path.write_text('{"v": 2', encoding="utf-8")  # cut short by a power cut
    assert read_json(path, dict) == {"v": 1}
    # The damaged file is moved aside, not lost and not overwritten later.
    broken = list(tmp_path.glob("data.json.broken-*"))
    assert len(broken) == 1 and broken[0].read_text() == '{"v": 2'
    assert not path.exists()


def test_finished_tmp_file_wins_over_the_backup(tmp_path):
    # A power cut between moving the old file to .bak and the new one into place.
    path = tmp_path / "data.json"
    (tmp_path / "data.json.bak").write_text('{"v": 1}')
    (tmp_path / "data.json.tmp").write_text('{"v": 2}')
    assert read_json(path, dict) == {"v": 2}
    # A half-written .tmp is skipped.
    (tmp_path / "data.json.tmp").write_text('{"v": ')
    assert read_json(path, dict) == {"v": 1}


def test_nothing_usable_reads_as_none(tmp_path):
    path = tmp_path / "data.json"
    assert read_json(path, dict) is None
    path.write_text("[]")
    assert read_json(path, lambda d: d["x"]) is None


def test_video_list_survives_a_damaged_file(tmp_path):
    with make_client(tmp_path) as c:
        add_fake_video(c, tmp_path, "aaa")
        add_fake_video(c, tmp_path, "bbb")
    (tmp_path / "videos.json").write_text('{"aaa": {"id": "aaa", "na', encoding="utf-8")
    with make_client(tmp_path) as c:
        # The previous version: written before "bbb" was added.
        assert video_ids(c) == {"aaa"}


def test_damaged_users_file_does_not_stop_the_service(tmp_path):
    (tmp_path / "users.json").write_text("garbage", encoding="utf-8")
    with make_client(tmp_path) as c:
        assert c.get("/api/me").json()["username"] == "admin"


@pytest.mark.parametrize(
    "name, content",
    [
        ("videos.json", {"aaa": {"id": "aaa", "name": "a.mp4"}}),
        ("videos.json", {"aaa": {"id": "aaa", "name": "a.mp4", "status": "ready", "created": "today"}}),
        ("users.json", {"olena": {"role": "viewer"}}),
        ("sessions.json", {"abc": {"username": "admin"}}),
        ("devices.json", {"abc": {"name": "Проектор"}}),
    ],
)
def test_file_of_the_wrong_shape_counts_as_damaged(tmp_path, name, content):
    (tmp_path / name).write_text(json.dumps(content), encoding="utf-8")
    with make_client(tmp_path) as c:
        assert c.get("/api/videos").json()["videos"] == []
        assert c.get("/api/users").status_code == 200
        assert c.get("/api/devices").json()["devices"] == []
    # Kept aside for a person to look at, not overwritten.
    assert list(tmp_path.glob(name + ".broken-*"))
