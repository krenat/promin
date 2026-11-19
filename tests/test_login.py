"""Logging in and out: the cookie, sessions, the limit on attempts."""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from helpers import (
    TOKEN,
    VIEWER_PASSWORD,
    add_user,
    another,
    change_stored,
    make_client,
    pair_key,
    sign_in,
    start_pairing,
    upload,
)
from promin.app import create_app
from promin.config import Settings


def test_pages_redirect_to_login(anon):
    for path in ("/", "/media/abc", "/thumb/abc"):
        r = anon.get(path)
        assert r.status_code == 303, path
        assert r.headers["location"] == "/login"
    # The projector page is open: without a login it only shows a QR code to pair.
    r = anon.get("/tv")
    assert r.status_code == 200
    assert "pair-qr" in r.text


def test_api_requires_login(anon, tmp_path):
    assert anon.get("/api/videos").status_code == 401
    assert anon.delete("/api/videos/abc").status_code == 401
    assert anon.post("/api/videos/abc/retry").status_code == 401
    assert upload(anon, b"x" * 100).status_code == 401
    assert list((tmp_path / "uploads").iterdir()) == []


def test_login_page_and_logo_are_public(anon):
    r = anon.get("/login")
    assert r.status_code == 200
    assert 'name="username"' in r.text
    assert 'name="password"' in r.text
    assert anon.get("/static/logo.svg").status_code == 200


def test_login_sets_long_lived_http_only_cookie(anon):
    r = sign_in(anon)
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert "max-age=2592000" in cookie
    assert TOKEN not in r.headers["set-cookie"]
    assert anon.get("/api/videos").status_code == 200
    assert anon.get("/login").status_code == 303  # already logged in


def test_wrong_token_is_rejected(anon):
    r = sign_in(anon, password="wrong-token-000000000")
    assert r.status_code == 401
    assert "Неправильний логін або пароль" in r.text
    assert "set-cookie" not in r.headers
    assert anon.get("/api/videos").status_code == 401


def test_forged_cookie_is_rejected(anon):
    anon.cookies.set("promin_session", "admin." + "0" * 64)
    assert anon.get("/api/videos").status_code == 401


def test_login_attempts_are_limited(anon):
    for _ in range(5):
        assert sign_in(anon, password="wrong").status_code == 401
    # After five failures even the right password is refused.
    assert sign_in(anon).status_code == 429
    assert anon.get("/api/videos").status_code == 401


def test_successful_login_does_not_reset_earlier_failures(client):
    assert add_user(client).status_code == 201
    anon = another(client)
    # A viewer's own successful login gives no new attempts at someone else's password.
    for _ in range(4):
        assert sign_in(anon, password="wrong").status_code == 401
    assert sign_in(anon, "olena", VIEWER_PASSWORD).status_code == 303
    anon.cookies.clear()
    assert sign_in(anon, password="wrong").status_code == 401
    assert sign_in(anon).status_code == 429


def test_successful_logins_are_not_counted(anon):
    for _ in range(8):
        assert sign_in(anon).status_code == 303
        anon.cookies.clear()


def test_login_attempt_is_counted_before_the_password_is_checked(anon):
    limiter_hits = []
    original = anon.app.state.services.users.get

    def spy(username):
        # At this moment the password is still being checked, yet the attempt is recorded.
        limiter_hits.append(sign_in_blocked_after(anon, 4))
        return original(username)

    anon.app.state.services.users.get = spy
    assert sign_in(anon, "nobody", "whatever-123").status_code == 401
    assert limiter_hits == [True]


def sign_in_blocked_after(client: TestClient, more: int) -> bool:
    """Whether login is blocked after `more` further failed attempts."""
    # Called from inside a request, so these attempts need an event loop of their own.
    other = TestClient(client.app, follow_redirects=False)
    for _ in range(more):
        assert sign_in(other, password="wrong").status_code == 401
    return sign_in(other).status_code == 429


def test_login_body_without_length_is_limited(anon):
    def body():
        yield b"username=admin&password="
        for _ in range(64):
            yield b"x" * 1024

    assert anon.post("/login", content=body()).status_code == 413
    assert anon.post("/login", content=b"x" * 5000).status_code == 413


def test_each_login_gets_its_own_session(anon, tmp_path):
    first = sign_in(anon).cookies["promin_session"]
    anon.cookies.clear()
    second = sign_in(anon).cookies["promin_session"]
    assert first != second
    # The data file holds only a hash of the secret.
    data = b"".join(path.read_bytes() for path in tmp_path.glob("promin.db*"))
    assert first.split(".", 1)[1].encode() not in data and TOKEN.encode() not in data


def test_logout_revokes_the_session_on_the_server(anon):
    cookie = sign_in(anon).cookies["promin_session"]
    assert anon.post("/logout").status_code == 303
    # Even if someone kept the cookie, it no longer works.
    anon.cookies.set("promin_session", cookie)
    assert anon.get("/api/me").status_code == 401


def test_logout_needs_the_real_cookie_to_revoke(anon):
    cookie = sign_in(anon).cookies["promin_session"]
    other = another(anon)
    other.cookies.set("promin_session", cookie.split(".", 1)[0] + ".guess")
    assert other.post("/logout").status_code == 303
    assert anon.get("/api/me").status_code == 200


def test_idle_session_expires(anon):
    cookie = sign_in(anon).cookies["promin_session"]
    sessions = anon.app.state.services.sessions
    session_id = cookie.split(".", 1)[0]
    # The session was used: the stamp is refreshed and it lives on.
    age = "UPDATE sessions SET seen = seen - ? WHERE id = ?"
    change_stored(anon, age, 29 * 24 * 3600, session_id)
    assert anon.get("/api/me").status_code == 200
    assert time.time() - sessions.get(session_id)["seen"] < 60
    # Unused for more than 30 days: the session is gone.
    change_stored(anon, age, 31 * 24 * 3600, session_id)
    assert anon.get("/api/me").status_code == 401
    assert sessions.get(session_id) is None


def test_opening_a_page_renews_the_cookie(anon):
    cookie = sign_in(anon).cookies["promin_session"]
    renewed = anon.get("/api/me").headers["set-cookie"].lower()
    assert renewed.startswith("promin_session=" + cookie.lower())
    assert "max-age=2592000" in renewed and "httponly" in renewed
    # Without a login there is nothing to renew.
    anon.cookies.clear()
    assert "set-cookie" not in anon.get("/api/me").headers


def test_changing_token_ends_admin_sessions(tmp_path):
    with make_client(tmp_path) as c:
        # Read first: /api/me renews the cookie, and the test client then holds it twice.
        cookie = c.cookies["promin_session"]
        assert c.get("/api/me").status_code == 200
    other = Settings(data_dir=tmp_path, token="another-token-1234567890")
    with TestClient(create_app(other)) as c:
        c.cookies.set("promin_session", cookie)
        assert c.get("/api/me").status_code == 401


def test_login_returns_to_tv_page(anon):
    assert 'name="next" value="/tv"' in anon.get("/login?next=/tv").text
    r = anon.post("/login", data={"username": "admin", "password": TOKEN, "next": "/tv"})
    assert r.status_code == 303 and r.headers["location"] == "/tv"
    assert anon.get("/tv").status_code == 200


def test_login_ignores_foreign_next(anon):
    assert 'value="/"' in anon.get("/login?next=https://evil.example/").text
    r = anon.post(
        "/login", data={"username": "admin", "password": TOKEN, "next": "//evil.example"}
    )
    assert r.headers["location"] == "/"


@pytest.mark.parametrize("next_path", ["/pair?t=short", "/pair?t=" + "a" * 32 + "&x=1", "/pair", "//evil.example/pair?t=" + "a" * 32])
def test_login_returns_only_to_a_well_formed_code(anon, next_path):
    r = anon.post("/login", data={"username": "admin", "password": TOKEN, "next": next_path})
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_cookies_are_secure_only_over_https(tmp_path):
    settings = Settings(data_dir=tmp_path, token=TOKEN)
    with TestClient(create_app(settings), follow_redirects=False) as plain:
        assert "secure" not in sign_in(plain).headers["set-cookie"].lower()
    with TestClient(create_app(settings), base_url="https://testserver", follow_redirects=False) as tls:
        assert "secure" in sign_in(tls).headers["set-cookie"].lower()
        # The projector's cookie follows the same rule.
        pairing = start_pairing(tls)
        tls.post("/api/pair/approve", json=pair_key(pairing))
        r = tls.post("/api/pair/status", json={"token": pairing["token"]})
        assert r.json()["status"] == "approved"
        assert "secure" in r.headers["set-cookie"].lower()


def test_limiter_forgets_addresses_that_never_return(monkeypatch):
    from promin import auth

    monkeypatch.setattr(auth, "LIMITER_MAX_ADDRESSES", 10)
    limiter = auth.LoginLimiter(max_failures=5, window=0.05)
    for i in range(10):
        limiter.fail("10.0.0.%d" % i)
    time.sleep(0.1)
    limiter.fail("10.0.1.1")
    assert list(limiter._failures) == ["10.0.1.1"]
