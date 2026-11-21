"""Connecting a projector by a short link typed on it."""
from __future__ import annotations

import io
import json
import time
import urllib.error

import pytest

from helpers import another, make_client, remote_closes
from promin import shortener
from promin.shortener import ALIAS_ALPHABET, IsGd, ShortenerError


class FakeShortener:
    """Stands in for the shortener: remembers what it was asked to shorten."""

    def __init__(self) -> None:
        self.asked: list = []
        self.fails = False

    def shorten(self, url: str) -> str:
        if self.fails:
            raise ShortenerError("down")
        self.asked.append(url)
        return "sho.rt/abc%d" % len(self.asked)


@pytest.fixture
def links(client) -> FakeShortener:
    fake = FakeShortener()
    client.app.state.services.shortener = fake
    return fake


def token_of(fake: FakeShortener) -> str:
    return fake.asked[-1].split("/c/")[1]


def test_a_short_link_makes_the_screen_a_projector(client, projector, links):
    r = client.post("/api/links")
    assert r.status_code == 201
    assert r.json()["short"] == "sho.rt/abc1" and r.json()["expires_in"] == 600
    # What was shortened is an address on this very server, with a token nobody can guess.
    assert links.asked[0].startswith("http://testserver/c/")
    token = token_of(links)
    assert len(token) >= 32

    # Opening the address spends nothing: a link previewer may fetch it before the projector.
    page = projector.get("/c/" + token)
    assert page.status_code == 200 and "tv.js" in page.text
    assert projector.get("/api/me").status_code == 401

    r = projector.post("/api/links/redeem", json={"token": token})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "max-age=2592000" in cookie
    assert projector.get("/api/me").json()["role"] == "device"
    devices = client.get("/api/devices").json()["devices"]
    assert [d["approved_by"] for d in devices] == ["admin"]


def test_a_link_works_only_once(client, projector, links):
    client.post("/api/links")
    token = token_of(links)
    assert projector.post("/api/links/redeem", json={"token": token}).status_code == 200
    # The address stays in the first screen's history; opened again or elsewhere it gives nothing.
    other = another(client)
    r = other.post("/api/links/redeem", json={"token": token})
    assert r.status_code == 404 and "set-cookie" not in r.headers
    assert other.get("/api/me").status_code == 401
    assert len(client.get("/api/devices").json()["devices"]) == 1


def test_unknown_and_expired_links_are_refused(client, projector, links, monkeypatch):
    assert projector.post("/api/links/redeem", json={"token": "x" * 32}).status_code == 404
    assert projector.post("/api/links/redeem", json={"token": ""}).status_code == 404
    assert projector.post("/api/links/redeem", json={}).status_code == 422
    client.post("/api/links")
    now = time.monotonic()
    monkeypatch.setattr("promin.devices.time.monotonic", lambda: now + 601)
    assert projector.post("/api/links/redeem", json={"token": token_of(links)}).status_code == 404
    assert client.get("/api/devices").json()["devices"] == []


def test_disconnecting_the_projector_ends_what_the_link_gave(client, projector, links):
    client.post("/api/links")
    token = token_of(links)
    projector.post("/api/links/redeem", json={"token": token})
    device_id = client.get("/api/devices").json()["devices"][0]["id"]
    with projector.websocket_connect("/ws?role=tv") as tv:
        assert client.delete("/api/devices/" + device_id).status_code == 204
        assert remote_closes(tv) == 4401
    assert projector.get("/api/me").status_code == 401
    assert projector.post("/api/links/redeem", json={"token": token}).status_code == 404


def test_a_link_can_be_taken_back_by_its_maker_or_an_administrator(client, viewer, projector, links):
    mine = viewer.post("/api/links").json()["id"]
    token = token_of(links)
    other = client.post("/api/links").json()["id"]
    # A viewer cancels only their own link; an administrator cancels anyone's.
    assert viewer.delete("/api/links/" + other).status_code == 404
    assert viewer.delete("/api/links/" + mine).status_code == 204
    assert viewer.delete("/api/links/" + mine).status_code == 404
    assert projector.post("/api/links/redeem", json={"token": token}).status_code == 404
    again = viewer.post("/api/links").json()["id"]
    assert client.delete("/api/links/" + again).status_code == 204


def test_a_projector_cannot_make_links(client, projector, links):
    client.post("/api/links")
    projector.post("/api/links/redeem", json={"token": token_of(links)})
    assert projector.post("/api/links").status_code == 403
    assert projector.delete("/api/links/abc").status_code == 403
    assert another(client).post("/api/links").status_code in (303, 401)


def test_links_are_off_without_a_shortener(client, viewer):
    assert client.get("/api/me").json()["links"] is False
    assert client.post("/api/links").status_code == 404


def test_the_page_is_told_that_links_are_on(client, viewer, links):
    assert client.get("/api/me").json()["links"] is True
    assert viewer.get("/api/me").json()["links"] is True


def test_a_failing_shortener_leaves_no_link(client, links):
    links.fails = True
    r = client.post("/api/links")
    assert r.status_code == 502 and "QR" in r.json()["detail"]
    assert client.app.state.services.links.pending == {}


def test_waiting_links_are_limited(client, links):
    for _ in range(20):
        assert client.post("/api/links").status_code == 201
    assert client.post("/api/links").status_code == 429
    assert len(links.asked) == 20


def test_the_shortener_is_chosen_by_the_settings(tmp_path):
    assert shortener.create("") is None
    assert isinstance(shortener.create("isgd"), IsGd)
    with pytest.raises(RuntimeError):
        make_client(tmp_path, login=False, shortener="nope")


def answers(monkeypatch, *replies):
    """Makes is.gd answer with these in turn; returns the addresses that were asked for."""
    asked: list = []
    queue = list(replies)

    def urlopen(url, timeout):
        asked.append(url)
        reply = queue.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return io.BytesIO(reply if isinstance(reply, bytes) else json.dumps(reply).encode())

    monkeypatch.setattr("promin.shortener.urllib.request.urlopen", urlopen)
    return asked


def test_isgd_is_asked_for_an_alias_that_is_easy_to_type(monkeypatch):
    asked = answers(monkeypatch, {"shorturl": "https://is.gd/abc234"})
    assert IsGd().shorten("http://1.2.3.4:8084/c/tok") == "is.gd/abc234"
    assert asked[0].startswith("https://is.gd/create.php?format=json&url=http%3A%2F%2F1.2.3.4%3A8084%2Fc%2Ftok&shorturl=")
    alias = asked[0].split("shorturl=")[1]
    assert len(alias) == 6 and set(alias) <= set(ALIAS_ALPHABET)


def test_isgd_tries_another_alias_when_one_is_taken(monkeypatch):
    taken = {"errorcode": 2, "errormessage": "taken"}
    asked = answers(monkeypatch, taken, {"shorturl": "https://is.gd/xyz789"})
    assert IsGd().shorten("http://a/c/t") == "is.gd/xyz789"
    assert len(asked) == 2 and asked[0] != asked[1]
    answers(monkeypatch, taken, taken, taken)
    with pytest.raises(ShortenerError):
        IsGd().shorten("http://a/c/t")


def test_isgd_errors_are_reported_not_retried(monkeypatch):
    refused = urllib.error.HTTPError(
        "u", 400, "Bad", None, io.BytesIO(b'{"errorcode": 1, "errormessage": "bad url"}')  # type: ignore[arg-type]
    )
    for reply in ({"errorcode": 3, "errormessage": "rate limit"}, refused, OSError("no network"), b"<html>"):
        asked = answers(monkeypatch, reply)
        with pytest.raises(ShortenerError):
            IsGd().shorten("http://a/c/t")
        assert len(asked) == 1
