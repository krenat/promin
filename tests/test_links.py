"""Connecting a projector by a short link typed on it."""
from __future__ import annotations

import http.client
import io
import json
import time
import urllib.error

import pytest

from helpers import another, make_client, remote_closes
from promin import shortener
from promin.devices import LINK_TTL
from promin.shortener import ALIAS_ALPHABET, ShortenerError, SpooMe, named


class FakeShortener:
    """Stands in for the shortener: remembers what it was asked to shorten."""

    def __init__(self) -> None:
        self.asked: list = []
        self.fails = False

    def shorten(self, url: str, ttl: int) -> str:
        assert ttl == LINK_TTL
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
    # The address with the token does not travel to the sites the page loads its fonts from.
    assert page.headers["referrer-policy"] == "no-referrer"
    assert '<meta name="referrer" content="no-referrer">' in page.text
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


def test_unknown_and_expired_links_are_refused(client, projector, links):
    assert projector.post("/api/links/redeem", json={"token": "x" * 32}).status_code == 404
    assert projector.post("/api/links/redeem", json={"token": ""}).status_code == 404
    assert projector.post("/api/links/redeem", json={}).status_code == 422
    link_id = client.post("/api/links").json()["id"]
    client.app.state.services.links.pending[link_id]["created"] -= LINK_TTL + 1
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


def test_a_browser_that_is_logged_in_keeps_its_login(client, viewer, projector, links):
    """Someone opens the link on their own phone: it must not turn into a projector."""
    client.post("/api/links")
    token = token_of(links)
    r = viewer.post("/api/links/redeem", json={"token": token})
    assert r.status_code == 409 and "set-cookie" not in r.headers
    assert viewer.get("/api/me").json()["role"] == "viewer"
    # The link was not spent on it, and a projector does not take a second link either.
    assert projector.post("/api/links/redeem", json={"token": token}).status_code == 200
    client.post("/api/links")
    assert projector.post("/api/links/redeem", json={"token": token_of(links)}).status_code == 409
    assert len(client.get("/api/devices").json()["devices"]) == 1


def test_the_phone_is_told_when_its_link_is_opened(client, projector, links):
    with client.websocket_connect("/ws?role=remote") as remote:
        link_id = client.post("/api/links").json()["id"]
        projector.post("/api/links/redeem", json={"token": token_of(links)})
        assert remote.receive_json() == {"type": "tv", "connected": False}
        assert remote.receive_json() == {"type": "link", "id": link_id}


def test_new_links_are_limited_even_when_each_is_cancelled(client, links):
    for _ in range(30):
        r = client.post("/api/links")
        assert r.status_code == 201
        assert client.delete("/api/links/" + r.json()["id"]).status_code == 204
    assert client.post("/api/links").status_code == 429
    assert len(links.asked) == 30


@pytest.mark.parametrize("host", ["localhost:8080", "127.0.0.1:8080", "[::1]:8080"])
def test_no_link_is_made_for_an_address_only_this_machine_knows(client, links, host):
    r = client.post("/api/links", headers={"Host": host})
    assert r.status_code == 400 and "localhost" in r.json()["detail"]
    assert links.asked == []


def test_the_shortener_is_chosen_by_the_settings(tmp_path):
    assert shortener.create("") is None
    assert isinstance(shortener.create("spoo"), SpooMe)
    # A wrong name switches links off; it does not stop the service.
    with make_client(tmp_path, shortener="nope") as c:
        assert c.get("/api/me").json()["links"] is False


class Answer(io.BytesIO):
    status = 201


def refusal(status: int, **fields) -> urllib.error.HTTPError:
    body = io.BytesIO(json.dumps(fields).encode())
    return urllib.error.HTTPError("u", status, "Refused", None, body)  # type: ignore[arg-type]


def answers(monkeypatch, *replies):
    """Makes spoo.me answer with these in turn; returns what it was sent each time."""
    asked: list = []
    queue = list(replies)

    def urlopen(request, timeout):
        assert request.full_url == "https://spoo.me/api/v1/shorten" and request.get_method() == "POST"
        asked.append(json.loads(request.data))
        reply = queue.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return Answer(reply if isinstance(reply, bytes) else json.dumps(reply).encode())

    monkeypatch.setattr("promin.shortener.urllib.request.urlopen", urlopen)
    return asked


def test_spoo_is_asked_for_a_link_that_is_easy_to_type_and_ends_by_itself(monkeypatch):
    asked = answers(monkeypatch, {"short_url": "https://spoo.me/abc23"})
    before = int(time.time())
    assert SpooMe().shorten("http://tv.example.org:8084/c/tok", 600) == "spoo.me/abc23"
    assert asked[0]["long_url"] == "http://tv.example.org:8084/c/tok"
    assert len(asked[0]["alias"]) == 5 and set(asked[0]["alias"]) <= set(ALIAS_ALPHABET)
    # Nobody can delete a link made without an account, so it is told when to end.
    assert before + 600 <= asked[0]["expire_after"] <= int(time.time()) + 600
    # An answer without the address still names the alias that was asked for.
    asked = answers(monkeypatch, {"status": "ACTIVE"})
    assert SpooMe().shorten("http://tv.example.org/c/tok", 600) == "spoo.me/" + asked[0]["alias"]


def test_a_bare_ip_address_is_given_a_name(monkeypatch):
    """spoo.me refuses an address with a bare IP in it."""
    asked = answers(monkeypatch, {"short_url": "https://spoo.me/abc23"})
    SpooMe().shorten("http://203.0.113.5:8084/c/tok", 600)
    assert asked[0]["long_url"] == "http://203.0.113.5.nip.io:8084/c/tok"
    assert named("http://203.0.113.5/c/tok") == "http://203.0.113.5.nip.io/c/tok"
    assert named("https://203.0.113.5:8443/c/tok?x=1") == "https://203.0.113.5.nip.io:8443/c/tok?x=1"
    for kept in ("http://tv.example.org:8084/c/tok", "http://[2001:db8::1]:8084/c/tok", "http://999.1.1.1/c/tok"):
        assert named(kept) == kept


def test_spoo_tries_another_alias_when_one_is_taken(monkeypatch):
    taken = refusal(409, error="Alias already exists", code="conflict", field="alias")
    asked = answers(monkeypatch, taken, {"short_url": "https://spoo.me/xyz78"})
    assert SpooMe().shorten("http://a.example/c/t", 600) == "spoo.me/xyz78"
    assert len(asked) == 2 and asked[0]["alias"] != asked[1]["alias"]
    asked = answers(monkeypatch, *[refusal(422, error="alias", field="alias") for _ in range(3)])
    with pytest.raises(ShortenerError):
        SpooMe().shorten("http://a.example/c/t", 600)
    assert len(asked) == 3


def test_spoo_errors_are_reported_not_retried(monkeypatch):
    unreadable = urllib.error.HTTPError("u", 502, "Bad", None, None)  # type: ignore[arg-type]
    for reply in (
        refusal(400, error="URL is not allowed or invalid", code="validation_error", field="long_url"),
        refusal(429, error="rate limit"), unreadable, http.client.IncompleteRead(b"{"),
        OSError("no network"), b"<html>", b"[]",
    ):
        asked = answers(monkeypatch, reply)
        with pytest.raises(ShortenerError):
            SpooMe().shorten("http://a.example/c/t", 600)
        assert len(asked) == 1
