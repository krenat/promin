"""The encoding settings page and its API."""
from __future__ import annotations

import pytest

from helpers import change_stored
from helpers import NEW_ENCODING, make_client, needs_ffmpeg, probe_video, upload, wait_done


def test_settings_default_to_the_environment(tmp_path):
    with make_client(tmp_path, max_bitrate_kbps=6000, x264_preset="fast") as c:
        data = c.get("/api/settings").json()
        assert data["encoding"] == data["defaults"]
        assert data["encoding"]["max_bitrate_kbps"] == 6000
        assert data["encoding"]["x264_preset"] == "fast"
        assert data["encoding"]["max_height"] == 1080 and data["encoding"]["crf"] == 21
        assert data["choices"]["max_height"] == [720, 1080, 1440, 2160]


def test_admin_saves_settings_and_they_survive_restart(tmp_path):
    with make_client(tmp_path) as c:
        r = c.put("/api/settings", json=NEW_ENCODING)
        assert r.status_code == 200 and r.json()["encoding"] == NEW_ENCODING
        assert c.get("/api/settings").json()["encoding"] == NEW_ENCODING
    with make_client(tmp_path) as c:
        data = c.get("/api/settings").json()
        assert data["encoding"] == NEW_ENCODING
        assert data["defaults"]["max_height"] == 1080  # the reset button still has the defaults


@pytest.mark.parametrize(
    "change",
    [
        {"max_height": 1000}, {"max_fps": 24}, {"max_bitrate_kbps": 10}, {"max_bitrate_kbps": 10**6},
        {"crf": 5}, {"crf": 40}, {"audio_bitrate_kbps": 1}, {"x264_preset": "placebo; rm -rf /"},
        {"crf": "high"},
    ],
)
def test_invalid_settings_are_rejected(client, change):
    before = client.get("/api/settings").json()["encoding"]
    assert client.put("/api/settings", json={**NEW_ENCODING, **change}).status_code in (400, 422)
    assert client.get("/api/settings").json()["encoding"] == before


def test_every_user_reaches_settings(client, viewer, anon):
    assert client.get("/settings").status_code == 200
    assert viewer.get("/settings").status_code == 200
    assert viewer.put("/api/settings", json=NEW_ENCODING).status_code == 200
    assert client.get("/api/settings").json()["encoding"] == NEW_ENCODING
    assert anon.get("/api/settings").status_code == 401
    assert anon.put("/api/settings", json=NEW_ENCODING).status_code == 401
    assert anon.get("/settings").status_code == 303


def test_stored_settings_that_no_longer_fit_fall_back_to_defaults(tmp_path):
    with make_client(tmp_path) as c:
        assert c.put("/api/settings", json=NEW_ENCODING).status_code == 200
        change_stored(c, "UPDATE settings SET value = ? WHERE key = 'encoding'", '{"max_height": 999}')
    with make_client(tmp_path) as c:
        assert c.get("/api/settings").json()["encoding"]["max_height"] == 1080
        change_stored(c, "UPDATE settings SET value = 'not json' WHERE key = 'encoding'")
    with make_client(tmp_path) as c:
        assert c.get("/api/settings").json()["encoding"]["max_height"] == 1080


@needs_ffmpeg
def test_saved_settings_apply_to_the_next_upload(client, tmp_path, big_clip):
    assert client.put("/api/settings", json={**NEW_ENCODING, "max_height": 720}).status_code == 200
    video = wait_done(client, upload(client, big_clip).json()["id"])
    assert video["status"] == "ready", video
    stream = probe_video(tmp_path / "videos" / (video["id"] + ".mp4"))
    assert (stream["width"], stream["height"]) == (1280, 720)
