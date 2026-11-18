"""Pairing a projector with a QR code, and what a paired projector may do."""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from helpers import change_stored
from helpers import (
    NEW_ENCODING,
    TOKEN,
    add_user,
    make_client,
    pair_key,
    pair_status,
    remote_closes,
    start_pairing,
    upload,
)


def test_projector_is_paired_by_qr_code(client, projector):
    assert projector.get("/api/me").status_code == 401
    pairing = start_pairing(projector)
    # The QR code is a picture of an address on this very server, with a key nobody can guess.
    assert pairing["url"].startswith("http://testserver/pair?t=")
    assert len(pair_key(pairing)["key"]) >= 32
    assert pairing["qr"].startswith("<svg") and "viewBox" in pairing["qr"]
    assert "key" not in pairing and "code" not in pairing
    assert pair_status(projector, pairing["token"]) == "pending"

    # The phone that scanned it is already logged in: it opens the page and confirms.
    page = client.get(pairing["url"])
    assert page.status_code == 200 and "Підключити проектор?" in page.text
    assert client.post("/api/pair/approve", json=pair_key(pairing)).status_code == 200

    r = projector.post("/api/pair/status", json={"token": pairing["token"]})
    assert r.json()["status"] == "approved"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "max-age=2592000" in cookie

    me = projector.get("/api/me").json()
    assert me["role"] == "device"
    with projector.websocket_connect("/ws?role=tv") as tv, client.websocket_connect("/ws?role=remote") as remote:
        assert remote.receive_json() == {"type": "tv", "connected": True}
        remote.send_json({"type": "command", "action": "pause"})
        assert tv.receive_json() == {"type": "command", "action": "pause"}


def test_code_works_only_once(client, projector):
    pairing = start_pairing(projector)
    assert client.post("/api/pair/approve", json=pair_key(pairing)).status_code == 200
    assert client.post("/api/pair/approve", json=pair_key(pairing)).status_code == 404
    assert pair_status(projector, pairing["token"]) == "approved"
    assert pair_status(projector, pairing["token"]) == "expired"


def test_wrong_or_unknown_code_is_rejected(client, projector):
    pairing = start_pairing(projector)
    assert client.post("/api/pair/approve", json={"key": "x" * 32}).status_code == 404
    assert client.post("/api/pair/approve", json={"key": ""}).status_code == 404
    # The polling token is the projector's own secret: it approves nothing.
    assert client.post("/api/pair/approve", json={"key": pairing["token"]}).status_code == 404
    assert client.post("/api/pair/approve", json={"code": "123456"}).status_code == 422
    assert pair_status(projector, pairing["token"]) == "pending"
    assert pair_status(projector, "no-such-token") == "expired"


def test_scanning_without_a_login_returns_to_the_code_after_it(projector):
    anon = TestClient(projector.app, follow_redirects=False)  # a phone that is not logged in
    pairing = start_pairing(projector)
    path = pairing["url"][len("http://testserver"):]
    r = anon.get(path)
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=" + path.replace("/", "%2F").replace("?", "%3F").replace("=", "%3D")
    assert 'name="next" value="%s"' % path in anon.get(r.headers["location"]).text
    r = anon.post("/login", data={"username": "admin", "password": TOKEN, "next": path})
    assert r.status_code == 303 and r.headers["location"] == path
    assert anon.post("/api/pair/approve", json=pair_key(pairing)).status_code == 200


def test_a_projector_cannot_open_the_confirmation_page(client, projector):
    pairing = start_pairing(projector)
    client.post("/api/pair/approve", json=pair_key(pairing))
    pair_status(projector, pairing["token"])
    r = projector.get("/pair?t=" + "a" * 32)
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_code_expires(client, projector):
    pairing = start_pairing(projector)
    client.app.state.services.pairing.pending[pairing["token"]]["created"] -= 11 * 60
    assert client.post("/api/pair/approve", json=pair_key(pairing)).status_code == 404
    assert pair_status(projector, pairing["token"]) == "expired"


def test_an_approval_nobody_collects_leaves_no_device(client, projector):
    pairing = start_pairing(projector)
    assert client.post("/api/pair/approve", json=pair_key(pairing)).status_code == 200
    # The projector page was closed before it asked: nothing is paired yet.
    assert client.get("/api/devices").json()["devices"] == []
    client.app.state.services.pairing.pending[pairing["token"]]["created"] -= 11 * 60
    assert pair_status(projector, pairing["token"]) == "expired"
    assert client.get("/api/devices").json()["devices"] == []


def test_an_idle_projector_is_forgotten(client, projector):
    pairing = start_pairing(projector)
    client.post("/api/pair/approve", json=pair_key(pairing))
    assert pair_status(projector, pairing["token"]) == "approved"
    device_id = client.get("/api/devices").json()["devices"][0]["id"]
    age = "UPDATE devices SET seen = seen - ? WHERE id = ?"
    # In touch within 30 days: the stamp is refreshed and the projector stays paired.
    change_stored(client, age, 29 * 24 * 3600, device_id)
    assert projector.get("/api/me").status_code == 200
    assert time.time() - client.get("/api/devices").json()["devices"][0]["seen"] < 60
    # Silent for longer: it is gone from the list without ever coming back.
    change_stored(client, age, 31 * 24 * 3600, device_id)
    assert client.get("/api/devices").json()["devices"] == []
    assert projector.get("/api/me").status_code == 401


def test_approving_requires_a_logged_in_user(client, projector, viewer):
    pairing = start_pairing(projector)
    assert projector.post("/api/pair/approve", json=pair_key(pairing)).status_code == 401
    # A viewer can pair a projector too.
    assert viewer.post("/api/pair/approve", json=pair_key(pairing)).status_code == 200
    assert pair_status(projector, pairing["token"]) == "approved"
    # But a projector itself cannot pair other projectors.
    other = TestClient(client.app, follow_redirects=False)
    second = start_pairing(other)
    assert projector.post("/api/pair/approve", json=pair_key(second)).status_code == 403


def test_projector_can_only_show(client, projector):
    pairing = start_pairing(projector)
    client.post("/api/pair/approve", json=pair_key(pairing))
    pair_status(projector, pairing["token"])

    assert projector.get("/api/videos").status_code == 200
    assert upload(projector, b"x" * 100).status_code == 403
    assert projector.delete("/api/videos/abc").status_code == 403
    assert projector.post("/api/videos/delete", json={"ids": ["abc"]}).status_code == 403
    assert projector.post("/api/videos/abc/retry").status_code == 403
    assert projector.get("/api/settings").status_code == 403
    assert projector.put("/api/settings", json=NEW_ENCODING).status_code == 403
    r = projector.get("/settings", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert projector.get("/api/users").status_code == 403
    assert projector.get("/api/devices").status_code == 403
    assert add_user(projector, "petro").status_code == 403


def test_admin_can_disconnect_projector(client, projector):
    pairing = start_pairing(projector)
    client.post("/api/pair/approve", json=pair_key(pairing))
    pair_status(projector, pairing["token"])

    devices = client.get("/api/devices").json()["devices"]
    assert len(devices) == 1
    assert devices[0]["approved_by"] == "admin"
    assert "hash" not in devices[0]

    with projector.websocket_connect("/ws?role=tv") as tv:
        assert client.delete("/api/devices/" + devices[0]["id"]).status_code == 204
        with pytest.raises(WebSocketDisconnect) as closed:
            tv.receive_json()
        assert closed.value.code == 4401
    assert projector.get("/api/me").status_code == 401
    assert client.get("/api/devices").json()["devices"] == []
    assert client.delete("/api/devices/" + devices[0]["id"]).status_code == 404


def test_forged_device_cookie_is_rejected(client, projector):
    pairing = start_pairing(projector)
    client.post("/api/pair/approve", json=pair_key(pairing))
    pair_status(projector, pairing["token"])
    device_id = client.get("/api/devices").json()["devices"][0]["id"]

    forged = TestClient(client.app, follow_redirects=False)
    forged.cookies.set("promin_session", "@%s.%s" % (device_id, "guess"))
    assert forged.get("/api/me").status_code == 401
    forged.cookies.set("promin_session", "@nope.guess")
    assert forged.get("/api/me").status_code == 401


def test_pairing_starts_are_limited(projector):
    for _ in range(10):
        assert projector.post("/api/pair/start").status_code == 200
    assert projector.post("/api/pair/start").status_code == 429


def test_devices_survive_restart(tmp_path):
    with make_client(tmp_path) as c:
        tv = TestClient(c.app, follow_redirects=False)
        pairing = start_pairing(tv)
        c.post("/api/pair/approve", json=pair_key(pairing))
        cookie = tv.post("/api/pair/status", json={"token": pairing["token"]}).cookies["promin_session"]
        # The device secret is not stored in the file as plain text.
        data = b"".join(path.read_bytes() for path in tmp_path.glob("promin.db*"))
        assert cookie.split(".", 1)[1].encode() not in data
    with make_client(tmp_path, login=False) as c:
        c.cookies.set("promin_session", cookie)
        assert c.get("/api/me").json()["role"] == "device"


def test_projector_cannot_act_as_a_remote(client, projector):
    pairing = start_pairing(projector)
    client.post("/api/pair/approve", json=pair_key(pairing))
    pair_status(projector, pairing["token"])
    with projector.websocket_connect("/ws?role=tv"):
        pass
    with projector.websocket_connect("/ws?role=remote") as ws:
        assert remote_closes(ws) == 1008
