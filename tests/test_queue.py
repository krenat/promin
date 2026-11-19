from __future__ import annotations

from fastapi.testclient import TestClient

from helpers import (
    add_fake_video,
    change_stored,
    make_client,
    pair_key,
    pair_status,
    start_pairing,
    stored,
    tv_state,
)


def queue_names(client: TestClient) -> list:
    return [item["video_id"] for item in client.get("/api/queue").json()["queue"]]


def test_queue_is_built_and_edited(client, tmp_path):
    for video_id in ("aaa", "bbb", "ccc"):
        add_fake_video(client, tmp_path, video_id)
    assert client.get("/api/queue").json() == {"queue": []}
    for video_id in ("aaa", "bbb", "ccc", "aaa"):
        r = client.post("/api/queue", json={"video_id": video_id})
        assert r.status_code == 201
    queue = r.json()["queue"]
    # The same video may be queued twice; each entry has its own id.
    assert [q["video_id"] for q in queue] == ["aaa", "bbb", "ccc", "aaa"]
    assert len({q["entry"] for q in queue}) == 4
    assert queue[0]["name"] == "aaa.mp4" and queue[0]["thumb"] is True

    r = client.post("/api/queue/%s/move" % queue[2]["entry"], json={"position": 0})
    assert [q["video_id"] for q in r.json()["queue"]] == ["ccc", "aaa", "bbb", "aaa"]
    # A position past either end is clamped.
    client.post("/api/queue/%s/move" % queue[2]["entry"], json={"position": 99})
    assert queue_names(client) == ["aaa", "bbb", "aaa", "ccc"]

    r = client.delete("/api/queue/" + queue[0]["entry"])
    assert [q["video_id"] for q in r.json()["queue"]] == ["bbb", "aaa", "ccc"]
    assert client.delete("/api/queue/" + queue[0]["entry"]).status_code == 404
    assert client.post("/api/queue/nope/move", json={"position": 0}).status_code == 404

    assert client.delete("/api/queue").json() == {"queue": []}


def test_only_ready_videos_can_be_queued(client, tmp_path):
    add_fake_video(client, tmp_path, "aaa", status="processing")
    assert client.post("/api/queue", json={"video_id": "aaa"}).status_code == 404
    assert client.post("/api/queue", json={"video_id": "nope"}).status_code == 404
    assert queue_names(client) == []


def test_queue_has_a_limit(client, tmp_path, monkeypatch):
    from promin import library as library_module
    monkeypatch.setattr(library_module, "QUEUE_MAX", 2)
    add_fake_video(client, tmp_path, "aaa")
    for _ in range(2):
        assert client.post("/api/queue", json={"video_id": "aaa"}).status_code == 201
    assert client.post("/api/queue", json={"video_id": "aaa"}).status_code == 409


def test_deleted_videos_leave_the_queue(client, tmp_path):
    for video_id in ("aaa", "bbb", "ccc"):
        add_fake_video(client, tmp_path, video_id)
        client.post("/api/queue", json={"video_id": video_id})
    client.delete("/api/videos/aaa")
    assert queue_names(client) == ["bbb", "ccc"]
    client.post("/api/videos/delete", json={"ids": ["ccc"]})
    assert queue_names(client) == ["bbb"]
    assert [row["video_id"] for row in stored(client, "SELECT video_id FROM queue")] == ["bbb"]


def test_queue_survives_restart(tmp_path):
    with make_client(tmp_path) as c:
        add_fake_video(c, tmp_path, "aaa")
        c.post("/api/queue", json={"video_id": "aaa"})
    with make_client(tmp_path) as c:
        assert queue_names(c) == ["aaa"]


def test_viewer_manages_the_queue_but_a_projector_does_not(client, viewer, projector, tmp_path):
    add_fake_video(client, tmp_path, "aaa")
    assert viewer.post("/api/queue", json={"video_id": "aaa"}).status_code == 201
    assert viewer.get("/api/queue").status_code == 200

    pairing = start_pairing(projector)
    client.post("/api/pair/approve", json=pair_key(pairing))
    pair_status(projector, pairing["token"])
    assert projector.get("/api/queue").status_code == 403
    assert projector.post("/api/queue", json={"video_id": "aaa"}).status_code == 403
    assert projector.delete("/api/queue").status_code == 403


def test_remotes_see_queue_changes(client, tmp_path):
    add_fake_video(client, tmp_path, "aaa")
    with client.websocket_connect("/ws?role=remote") as remote:
        assert remote.receive_json() == {"type": "tv", "connected": False}
        client.post("/api/queue", json={"video_id": "aaa"})
        update = remote.receive_json()
        assert update["type"] == "queue" and [q["video_id"] for q in update["queue"]] == ["aaa"]
    # A remote that connects later gets the queue right away.
    with client.websocket_connect("/ws?role=remote") as remote:
        remote.receive_json()
        assert remote.receive_json()["type"] == "queue"


def test_next_plays_the_queue_in_order(client, tmp_path):
    for video_id in ("aaa", "bbb"):
        add_fake_video(client, tmp_path, video_id)
        client.post("/api/queue", json={"video_id": video_id})
    with client.websocket_connect("/ws?role=tv") as tv, client.websocket_connect("/ws?role=remote") as remote:
        remote.receive_json()  # tv connected
        remote.receive_json()  # the queue
        remote.send_json({"type": "command", "action": "next"})
        assert tv.receive_json() == {"type": "command", "action": "load", "video_id": "aaa", "name": "aaa.mp4"}
        assert [q["video_id"] for q in remote.receive_json()["queue"]] == ["bbb"]

        # The projector reports the end of the video it plays: the next one starts by itself.
        tv.send_json(tv_state(video_id="aaa", name="aaa.mp4"))
        remote.receive_json()  # state
        tv.send_json({"type": "ended", "video_id": "aaa"})
        assert tv.receive_json()["video_id"] == "bbb"
        assert remote.receive_json() == {"type": "queue", "queue": []}

        # The queue is empty now: "next" says so.
        remote.send_json({"type": "command", "action": "next"})
        assert remote.receive_json() == {"type": "error", "message": "Черга порожня"}
    assert queue_names(client) == []


def test_a_stale_end_report_does_not_skip_a_video(client, tmp_path):
    for video_id in ("aaa", "bbb"):
        add_fake_video(client, tmp_path, video_id)
        client.post("/api/queue", json={"video_id": video_id})
    with client.websocket_connect("/ws?role=tv") as tv:
        tv.send_json(tv_state(video_id="zzz", name="zzz.mp4"))
        # Not what is playing, and nothing playing at all: both ignored.
        tv.send_json({"type": "ended", "video_id": "old"})
        tv.send_json(tv_state())
        tv.send_json({"type": "ended", "video_id": "zzz"})
        tv.send_json(tv_state(video_id="zzz", name="zzz.mp4"))
        tv.send_json({"type": "ended", "video_id": "zzz"})
        assert tv.receive_json()["video_id"] == "aaa"
    assert queue_names(client) == ["bbb"]


def test_next_skips_videos_deleted_from_the_queue_file(client, tmp_path):
    add_fake_video(client, tmp_path, "gone")
    add_fake_video(client, tmp_path, "bbb")
    client.post("/api/queue", json={"video_id": "gone"})
    client.post("/api/queue", json={"video_id": "bbb"})
    # A video that vanished behind the server's back: its queue entry is still there.
    change_stored(client, "DELETE FROM videos WHERE id = 'gone'")
    with client.websocket_connect("/ws?role=tv") as tv, client.websocket_connect("/ws?role=remote") as remote:
        remote.receive_json()
        remote.receive_json()
        remote.send_json({"type": "command", "action": "next"})
        assert tv.receive_json()["video_id"] == "bbb"
    assert queue_names(client) == []


def test_tv_page_reports_the_end_of_a_video(client):
    page = client.get("/static/js/tv.js").text
    assert "type: 'ended'" in page
    assert "addEventListener('ended', finished)" in page


def test_tv_page_retries_a_failed_video_before_giving_up(client):
    page = client.get("/static/js/tv.js").text
    # An error is not the end of the video: a network drop must not start the next one.
    assert "addEventListener('error', failed)" in page
    assert "addEventListener('error', finished)" not in page
