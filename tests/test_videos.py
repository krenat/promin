"""The video library through the API: upload, playback, retry, deletion."""
from __future__ import annotations

import json
import time

import pytest

from helpers import (
    add_fake_video,
    count_commits,
    make_client,
    needs_ffmpeg,
    probe_video,
    upload,
    video_ids,
    wait_done,
)


def test_index_page(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Promin" in r.text


@needs_ffmpeg
def test_upload_process_play_delete(client, tmp_path, small_clip):
    r = upload(client, small_clip, name="%D0%9C%D0%BE%D1%80%D0%B5.mov")
    assert r.status_code == 201
    created = r.json()
    assert created["name"] == "Море.mov"
    assert created["status"] == "queued"

    video = wait_done(client, created["id"])
    assert video["status"] == "ready", video["error"]
    assert video["thumb"] is True
    assert 0.9 < video["duration"] < 1.2

    # The original is gone; the finished file is named by the server.
    assert list((tmp_path / "uploads").iterdir()) == []
    ready = tmp_path / "videos" / (video["id"] + ".mp4")
    assert [p.name for p in (tmp_path / "videos").iterdir()] == [ready.name]
    data = ready.read_bytes()

    full = client.get("/media/" + video["id"])
    assert full.status_code == 200
    assert full.headers["content-type"] == "video/mp4"
    assert full.content == data

    part = client.get("/media/" + video["id"], headers={"Range": "bytes=100-199"})
    assert part.status_code == 206
    assert part.content == data[100:200]
    assert part.headers["content-range"] == "bytes 100-199/%d" % len(data)

    thumb = client.get("/thumb/" + video["id"])
    assert thumb.status_code == 200
    assert thumb.content[:2] == b"\xff\xd8"  # JPEG

    assert client.delete("/api/videos/" + video["id"]).status_code == 204
    assert client.get("/api/videos").json()["videos"] == []
    assert client.get("/media/" + video["id"]).status_code == 404
    assert client.get("/thumb/" + video["id"]).status_code == 404
    for folder in ("uploads", "videos", "thumbs"):
        assert list((tmp_path / folder).iterdir()) == []


@needs_ffmpeg
def test_large_video_is_scaled_down(client, tmp_path, big_clip):
    created = upload(client, big_clip).json()
    video = wait_done(client, created["id"])
    assert video["status"] == "ready", video["error"]
    info = probe_video(tmp_path / "videos" / (video["id"] + ".mp4"))
    assert info["codec_name"] == "h264"
    assert info["pix_fmt"] == "yuv420p"
    assert (info["width"], info["height"]) == (1920, 1080)


@needs_ffmpeg
def test_broken_file_gets_error_and_can_be_retried(client, tmp_path):
    created = upload(client, b"this is not a video" * 100).json()
    video = wait_done(client, created["id"])
    assert video["status"] == "error"
    assert video["error"]
    assert client.get("/media/" + video["id"]).status_code == 404
    # The original stays so that processing can be retried.
    assert len(list((tmp_path / "uploads").iterdir())) == 1

    assert client.post("/api/videos/%s/retry" % video["id"]).status_code == 200
    assert wait_done(client, video["id"])["status"] == "error"


@needs_ffmpeg
def test_retry_is_only_for_failed_videos(client, small_clip):
    created = upload(client, small_clip).json()
    wait_done(client, created["id"])
    assert client.post("/api/videos/%s/retry" % created["id"]).status_code == 409
    assert client.post("/api/videos/nope/retry").status_code == 404


@needs_ffmpeg
def test_delete_while_waiting_in_queue(client, tmp_path, big_clip, small_clip):
    first = upload(client, big_clip).json()
    second = upload(client, small_clip).json()
    assert client.delete("/api/videos/" + second["id"]).status_code == 204
    assert client.delete("/api/videos/" + first["id"]).status_code == 204
    time.sleep(1.5)  # the worker has to notice the deletion and clean up
    assert client.get("/api/videos").json()["videos"] == []
    for folder in ("uploads", "videos", "thumbs"):
        assert list((tmp_path / folder).iterdir()) == []


@needs_ffmpeg
def test_unfinished_jobs_resume_after_restart(tmp_path, small_clip):
    (tmp_path / "uploads").mkdir()
    (tmp_path / "videos").mkdir()
    (tmp_path / "uploads" / "abc.mp4").write_bytes(small_clip)
    (tmp_path / "uploads" / "leftover.part").write_bytes(b"x")
    (tmp_path / "videos" / "abc.tmp.mp4").write_bytes(b"half-written")
    item = {
        "id": "abc", "name": "clip.mp4", "size": len(small_clip), "status": "processing",
        "created": 1.0, "source": "abc.mp4", "duration": None, "thumb": False, "error": None,
    }
    (tmp_path / "videos.json").write_text(json.dumps({"abc": item}), encoding="utf-8")

    with make_client(tmp_path) as c:
        assert not (tmp_path / "uploads" / "leftover.part").exists()
        assert wait_done(c, "abc")["status"] == "ready"
        assert not (tmp_path / "videos" / "abc.tmp.mp4").exists()


def test_file_name_cannot_escape_data_dir(client, tmp_path):
    r = upload(client, b"x" * 100, name="..%2F..%2Fevil.mp4")
    assert r.status_code == 201
    assert r.json()["name"] == "evil.mp4"
    stored = [p.name for p in (tmp_path / "uploads").iterdir()]
    assert all(name.startswith(r.json()["id"]) for name in stored)


def test_rejects_unsupported_type(client):
    assert upload(client, b"hello", name="notes.txt").status_code == 415


def test_rejects_empty_file(client):
    assert upload(client, b"").status_code == 400


def test_rejects_too_large_file(tmp_path):
    with make_client(tmp_path, max_upload_bytes=1000) as c:
        assert upload(c, b"x" * 2000).status_code == 413
        assert list((tmp_path / "uploads").iterdir()) == []


def test_rejects_when_disk_is_full(tmp_path):
    with make_client(tmp_path, disk_reserve_bytes=10**18) as c:
        assert upload(c, b"x" * 100).status_code == 507


def test_delete_unknown_video(client):
    assert client.delete("/api/videos/nope").status_code == 404


def test_admin_deletes_several_videos_at_once(client, tmp_path):
    for video_id in ("aaa", "bbb", "ccc"):
        add_fake_video(client, tmp_path, video_id)
    r = client.post("/api/videos/delete", json={"ids": ["aaa", "ccc", "aaa"]})
    assert r.status_code == 200
    assert r.json() == {"deleted": ["aaa", "ccc"], "missing": []}
    assert video_ids(client) == {"bbb"}
    left = sorted(p.name for p in (tmp_path / "videos").iterdir()) + sorted(
        p.name for p in (tmp_path / "thumbs").iterdir()
    )
    assert left == ["bbb.mp4", "bbb.jpg"]


def test_batch_delete_reports_videos_already_gone(client, tmp_path):
    add_fake_video(client, tmp_path, "aaa")
    r = client.post("/api/videos/delete", json={"ids": ["aaa", "nope"]})
    assert r.status_code == 200
    assert r.json() == {"deleted": ["aaa"], "missing": ["nope"]}


def test_batch_delete_also_removes_videos_in_processing(client, tmp_path):
    add_fake_video(client, tmp_path, "aaa", status="queued")
    add_fake_video(client, tmp_path, "bbb", status="error")
    assert client.post("/api/videos/delete", json={"ids": ["aaa", "bbb"]}).json()["missing"] == []
    assert video_ids(client) == set()


def test_batch_delete_ids_never_reach_the_file_system(client, tmp_path):
    add_fake_video(client, tmp_path, "aaa")
    # A glob pattern is not a known id, so no file is touched.
    r = client.post("/api/videos/delete", json={"ids": ["*", "../*", "a*"]})
    assert r.json() == {"deleted": [], "missing": ["*", "../*", "a*"]}
    assert (tmp_path / "videos" / "aaa.mp4").is_file()


@pytest.mark.parametrize("payload", [{"ids": []}, {"ids": [str(i) for i in range(501)]}, {}, {"ids": "aaa"}])
def test_invalid_batch_delete_is_rejected(client, tmp_path, payload):
    add_fake_video(client, tmp_path, "aaa")
    assert client.post("/api/videos/delete", json=payload).status_code in (400, 422)
    assert video_ids(client) == {"aaa"}


def test_only_admin_deletes_in_batches(client, viewer, anon, tmp_path):
    add_fake_video(client, tmp_path, "aaa")
    assert viewer.post("/api/videos/delete", json={"ids": ["aaa"]}).status_code == 403
    assert anon.post("/api/videos/delete", json={"ids": ["aaa"]}).status_code == 401
    assert video_ids(client) == {"aaa"}
    # A viewer still deletes one video at a time.
    assert viewer.delete("/api/videos/aaa").status_code == 204


def test_batch_delete_is_stored_at_once(client, tmp_path):
    """The videos leave the list and the watch queue together, in one transaction."""
    for i in range(20):
        add_fake_video(client, tmp_path, "v%02d" % i)
        client.post("/api/queue", json={"video_id": "v%02d" % i})
    commits = count_commits(client)
    r = client.post("/api/videos/delete", json={"ids": ["v%02d" % i for i in range(20)]})
    assert len(r.json()["deleted"]) == 20
    assert len(commits) == 1
    assert client.get("/api/queue").json() == {"queue": []}
    assert video_ids(client) == set()
