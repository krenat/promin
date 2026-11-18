"""Fixtures shared by the test files."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from helpers import VIEWER_PASSWORD, add_user, make_client, make_clip, sign_in


@pytest.fixture(autouse=True)
def fast_password_hashing(monkeypatch):
    """The real number of rounds takes a few tenths of a second per password, and the
    tests hash hundreds. The legacy number stays below it, as in production."""
    monkeypatch.setattr("promin.crypto.PBKDF2_ROUNDS", 2000)
    monkeypatch.setattr("promin.crypto.LEGACY_PBKDF2_ROUNDS", 1000)


@pytest.fixture(scope="session")
def small_clip(tmp_path_factory) -> bytes:
    return make_clip(tmp_path_factory.mktemp("clips") / "small.mp4")


@pytest.fixture(scope="session")
def big_clip(tmp_path_factory) -> bytes:
    return make_clip(tmp_path_factory.mktemp("clips") / "big.mp4", size="2560x1440")


@pytest.fixture
def client(tmp_path: Path):
    with make_client(tmp_path) as c:
        yield c


@pytest.fixture
def anon(tmp_path: Path):
    with make_client(tmp_path, login=False) as c:
        c.follow_redirects = False
        yield c


@pytest.fixture
def viewer(client, tmp_path):
    """A client logged in as a viewer; `client` alongside stays the administrator."""
    assert add_user(client).status_code == 201
    c = TestClient(client.app, follow_redirects=False)
    assert sign_in(c, "olena", VIEWER_PASSWORD).status_code == 303
    return c


@pytest.fixture
def projector(client):
    """A client without a login that plays the projector browser."""
    return TestClient(client.app, follow_redirects=False)
