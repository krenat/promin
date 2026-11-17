"""Users and what each role may do."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from helpers import VIEWER_PASSWORD, add_user, make_client, needs_ffmpeg, sign_in, upload, wait_done


def test_me_reports_role(client, viewer):
    assert client.get("/api/me").json() == {"username": "admin", "role": "admin"}
    assert viewer.get("/api/me").json() == {"username": "olena", "role": "viewer"}


def test_admin_manages_users(client, tmp_path):
    assert add_user(client).status_code == 201
    names = [u["username"] for u in client.get("/api/users").json()["users"]]
    assert names == ["admin", "olena"]
    # The password is not stored in the file as plain text.
    assert VIEWER_PASSWORD not in (tmp_path / "users.json").read_text(encoding="utf-8")

    assert client.delete("/api/users/olena").status_code == 204
    assert client.delete("/api/users/olena").status_code == 404
    assert [u["username"] for u in client.get("/api/users").json()["users"]] == ["admin"]


@pytest.mark.parametrize(
    "payload, status",
    [
        ({"username": "admin", "password": "long-enough-1"}, 409),
        ({"username": "A b", "password": "long-enough-1"}, 400),
        ({"username": "x", "password": "long-enough-1"}, 400),
        ({"username": "petro", "password": "short"}, 400),
        ({"username": "petro", "password": "long-enough-1", "role": "king"}, 400),
    ],
)
def test_invalid_users_are_rejected(client, payload, status):
    assert client.post("/api/users", json=payload).status_code == status


def test_duplicate_user_is_rejected(client):
    assert add_user(client).status_code == 201
    assert add_user(client).status_code == 409


def test_builtin_admin_is_protected(client):
    assert client.delete("/api/users/admin").status_code == 400
    r = client.post("/api/users/admin/password", json={"password": "another-pass-1"})
    assert r.status_code == 400


def test_admin_role_user_cannot_delete_self(client):
    assert add_user(client, "ivan", "ivan-password-1", role="admin").status_code == 201
    ivan = TestClient(client.app, follow_redirects=False)
    assert sign_in(ivan, "ivan", "ivan-password-1").status_code == 303
    assert ivan.get("/api/users").status_code == 200
    assert ivan.delete("/api/users/ivan").status_code == 400


@needs_ffmpeg
def test_viewer_watches_adds_and_deletes_videos(client, viewer, small_clip):
    video = wait_done(client, upload(client, small_clip).json()["id"])
    assert video["status"] == "ready"

    assert [v["id"] for v in viewer.get("/api/videos").json()["videos"]] == [video["id"]]
    assert viewer.get("/").status_code == 200
    assert viewer.get("/media/" + video["id"]).status_code == 200
    assert viewer.get("/thumb/" + video["id"]).status_code == 200

    # The library is shared: a viewer adds videos and deletes any of them.
    own = upload(viewer, small_clip)
    assert own.status_code == 201
    # Poll with `client`: only its requests run in the event loop that owns the queue.
    assert wait_done(client, own.json()["id"])["status"] == "ready"
    assert viewer.delete("/api/videos/" + video["id"]).status_code == 204
    assert [v["id"] for v in client.get("/api/videos").json()["videos"]] == [own.json()["id"]]


def test_viewer_can_retry_a_failed_video(client, viewer):
    video = wait_done(client, upload(client, b"not a video" * 10).json()["id"])
    assert video["status"] == "error"
    assert viewer.post("/api/videos/%s/retry" % video["id"]).status_code == 200


def test_viewer_cannot_manage_users(viewer):
    assert viewer.get("/api/users").status_code == 403
    assert add_user(viewer, "petro").status_code == 403
    assert viewer.delete("/api/users/olena").status_code == 403
    r = viewer.get("/users")
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_users_page_for_admin(client):
    r = client.get("/users")
    assert r.status_code == 200
    assert "Користувачі" in r.text


def test_deleting_user_ends_their_session(client, viewer):
    assert viewer.get("/api/videos").status_code == 200
    assert client.delete("/api/users/olena").status_code == 204
    assert viewer.get("/api/videos").status_code == 401


def test_password_change_ends_old_session(client, viewer):
    r = client.post("/api/users/olena/password", json={"password": "brand-new-pass-2"})
    assert r.status_code == 204
    assert viewer.get("/api/videos").status_code == 401

    fresh = TestClient(client.app, follow_redirects=False)
    assert sign_in(fresh, "olena", VIEWER_PASSWORD).status_code == 401
    assert sign_in(fresh, "olena", "brand-new-pass-2").status_code == 303
    assert fresh.get("/api/videos").status_code == 200


def test_unknown_user_and_wrong_password_look_the_same(anon):
    a = sign_in(anon, "nobody", "whatever-123")
    b = sign_in(anon, "admin", "whatever-123")
    assert a.status_code == b.status_code == 401
    assert a.text == b.text


def test_login_is_case_insensitive_for_username(client):
    assert add_user(client).status_code == 201
    c = TestClient(client.app, follow_redirects=False)
    assert sign_in(c, " Olena ", VIEWER_PASSWORD).status_code == 303


def test_logout_clears_session(client):
    c = TestClient(client.app, follow_redirects=False)
    assert sign_in(c).status_code == 303
    assert c.get("/api/me").status_code == 200
    r = c.post("/logout")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert c.get("/api/me").status_code == 401


def test_users_survive_restart(tmp_path):
    with make_client(tmp_path) as c:
        assert add_user(c).status_code == 201
    with make_client(tmp_path, login=False) as c:
        c.follow_redirects = False
        assert sign_in(c, "olena", VIEWER_PASSWORD).status_code == 303
