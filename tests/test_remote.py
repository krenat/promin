"""The remote: the projector page and the phones over WebSocket."""
from __future__ import annotations

import pytest
from starlette.websockets import WebSocketDisconnect

from helpers import needs_ffmpeg, pair_key, pair_status, remote_closes, start_pairing, tv_state, upload, wait_done


def test_websocket_requires_login(anon):
    with anon.websocket_connect("/ws?role=remote") as ws:
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    assert closed.value.code == 4401


def test_websocket_rejects_foreign_origin(client):
    # A page from another site or another port of this server cannot open the remote.
    for origin in ("http://evil.example", "http://testserver:8081"):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws?role=remote", headers={"origin": origin}):
                pass
    with client.websocket_connect("/ws?role=remote", headers={"origin": "http://testserver"}) as ws:
        assert ws.receive_json() == {"type": "tv", "connected": False}


def test_remote_sees_projector_come_and_go(client):
    with client.websocket_connect("/ws?role=remote") as remote:
        assert remote.receive_json() == {"type": "tv", "connected": False}
        with client.websocket_connect("/ws?role=tv") as tv:
            assert remote.receive_json() == {"type": "tv", "connected": True}
            tv.send_json(tv_state(position=12.5, duration=60, paused=False, video_id="abc", name="Clip"))
            state = remote.receive_json()
            assert state["type"] == "state"
            assert (state["video_id"], state["position"], state["paused"]) == ("abc", 12.5, False)
        assert remote.receive_json() == {"type": "tv", "connected": False}


def test_new_remote_gets_current_state(client):
    with client.websocket_connect("/ws?role=tv") as tv:
        with client.websocket_connect("/ws?role=remote") as first:
            assert first.receive_json()["connected"] is True
            tv.send_json(tv_state(video_id="abc", name="Clip", position=5))
            assert first.receive_json()["position"] == 5
            with client.websocket_connect("/ws?role=remote") as second:
                assert second.receive_json() == {"type": "tv", "connected": True}
                assert second.receive_json()["video_id"] == "abc"


def test_commands_without_projector_report_error(client):
    with client.websocket_connect("/ws?role=remote") as remote:
        remote.receive_json()
        remote.send_json({"type": "command", "action": "pause"})
        assert remote.receive_json()["type"] == "error"


@needs_ffmpeg
def test_remote_controls_projector(client, viewer, small_clip):
    video = wait_done(client, upload(client, small_clip, name="party.mp4").json()["id"])
    # A viewer can control playback too.
    with client.websocket_connect("/ws?role=tv") as tv, viewer.websocket_connect("/ws?role=remote") as remote:
        assert remote.receive_json()["connected"] is True

        remote.send_json({"type": "command", "action": "load", "video_id": video["id"]})
        assert tv.receive_json() == {
            "type": "command", "action": "load", "video_id": video["id"], "name": "party.mp4",
        }
        remote.send_json({"type": "command", "action": "seek", "value": 42})
        assert tv.receive_json() == {"type": "command", "action": "seek", "value": 42.0}
        remote.send_json({"type": "command", "action": "seek_by", "value": -10})
        assert tv.receive_json() == {"type": "command", "action": "seek_by", "value": -10.0}
        remote.send_json({"type": "command", "action": "volume", "value": 0.4})
        assert tv.receive_json() == {"type": "command", "action": "volume", "value": 0.4}
        for action in ("pause", "play", "stop"):
            remote.send_json({"type": "command", "action": action})
            assert tv.receive_json() == {"type": "command", "action": action}

        # An unknown video, an unknown action and garbage never reach the projector.
        remote.send_json({"type": "command", "action": "load", "video_id": "nope"})
        assert remote.receive_json() == {"type": "error", "message": "Це відео недоступне"}
        remote.send_json({"type": "command", "action": "format_disk"})
        remote.send_json({"type": "command", "action": "seek", "value": "soon"})
        remote.send_json({"type": "command", "action": "pause"})
        assert tv.receive_json() == {"type": "command", "action": "pause"}


def test_second_projector_replaces_first(client):
    with client.websocket_connect("/ws?role=remote") as remote:
        remote.receive_json()
        with client.websocket_connect("/ws?role=tv") as first:
            assert remote.receive_json()["connected"] is True
            with client.websocket_connect("/ws?role=tv") as second:
                with pytest.raises(WebSocketDisconnect) as closed:
                    first.receive_json()
                assert closed.value.code == 4000
                assert remote.receive_json()["connected"] is True
                # Commands now go to the new projector.
                remote.send_json({"type": "command", "action": "play"})
                assert second.receive_json() == {"type": "command", "action": "play"}


def test_a_phone_can_disconnect_the_projector(client):
    with client.websocket_connect("/ws?role=remote") as remote:
        remote.receive_json()
        with client.websocket_connect("/ws?role=tv") as tv:
            assert remote.receive_json()["connected"] is True
            remote.send_json({"type": "command", "action": "disconnect"})
            with pytest.raises(WebSocketDisconnect) as closed:
                tv.receive_json()
            assert closed.value.code == 4001
            assert remote.receive_json() == {"type": "tv", "connected": False}
            # It is gone at once: the next command has no projector to go to.
            remote.send_json({"type": "command", "action": "play"})
            assert remote.receive_json() == {"type": "error", "message": "Проектор не на зв'язку"}
        # The screen that was sent away may come back by itself.
        with client.websocket_connect("/ws?role=tv"):
            assert remote.receive_json() == {"type": "tv", "connected": True}


def test_a_disconnected_projector_stays_paired(client, projector):
    token = start_pairing(projector)
    client.post("/api/pair/approve", json=pair_key(token))
    assert pair_status(projector, token["token"]) == "approved"
    with client.websocket_connect("/ws?role=remote") as remote:
        remote.receive_json()
        with projector.websocket_connect("/ws?role=tv") as tv:
            remote.receive_json()
            remote.send_json({"type": "command", "action": "disconnect"})
            with pytest.raises(WebSocketDisconnect):
                tv.receive_json()
    assert len(client.get("/api/devices").json()["devices"]) == 1
    assert projector.get("/api/me").json()["role"] == "device"


def test_projector_state_is_sanitized(client):
    with client.websocket_connect("/ws?role=remote") as remote:
        remote.receive_json()
        with client.websocket_connect("/ws?role=tv") as tv:
            remote.receive_json()
            tv.send_json({"type": "state", "position": "abc", "volume": 7, "extra": "<script>", "name": "x" * 500})
            state = remote.receive_json()
            assert state["position"] == 0
            assert state["volume"] == 1
            assert "extra" not in state
            assert len(state["name"]) == 200


def test_a_failed_connection_is_logged(client, caplog):
    with client.websocket_connect("/ws?role=remote") as ws:
        ws.receive_json()
        ws.send_text("not json")
        with pytest.raises(Exception):
            ws.receive_json()
    assert "A remote connection failed" in caplog.text


def test_tv_page_asks_for_fullscreen_on_start(anon):
    page = anon.get("/static/js/tv.js").text
    assert "requestFullscreen" in page and "webkitRequestFullscreen" in page


def test_deleted_user_loses_the_remote_at_once(client, viewer):
    with viewer.websocket_connect("/ws?role=remote") as remote, \
            client.websocket_connect("/ws?role=remote") as admin_remote:
        remote.receive_json()
        admin_remote.receive_json()
        assert client.delete("/api/users/olena").status_code == 204
        assert remote_closes(remote) == 4401
        # The administrator's own remote stays.
        admin_remote.send_json({"type": "command", "action": "pause"})
        assert admin_remote.receive_json()["type"] == "error"  # no projector, but connected


def test_password_change_closes_the_remote(client, viewer):
    with viewer.websocket_connect("/ws?role=remote") as remote:
        remote.receive_json()
        client.post("/api/users/olena/password", json={"password": "brand-new-pass-2"})
        assert remote_closes(remote) == 4401


def test_logout_closes_remotes_of_that_session(client, viewer):
    with viewer.websocket_connect("/ws?role=remote") as remote:
        remote.receive_json()
        assert viewer.post("/logout").status_code == 303
        assert remote_closes(remote) == 4401
