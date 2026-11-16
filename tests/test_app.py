from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main import Settings, create_app  # noqa: E402

PAYLOAD = bytes(range(256)) * 40  # 10 240 байтів


def make_client(tmp_path: Path, **overrides) -> TestClient:
    settings = Settings(data_dir=tmp_path, disk_reserve_bytes=0, **overrides)
    return TestClient(create_app(settings))


@pytest.fixture
def client(tmp_path: Path):
    with make_client(tmp_path) as c:
        yield c


def upload(client: TestClient, name: str = "clip.mp4", data: bytes = PAYLOAD):
    return client.post("/api/videos", content=data, headers={"X-Filename": name})


def test_index_page(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Promin" in r.text


def test_upload_list_play_delete(client, tmp_path):
    r = upload(client)
    assert r.status_code == 201
    video = r.json()
    assert video["name"] == "clip.mp4"
    assert video["size"] == len(PAYLOAD)
    assert video["status"] == "ready"

    listing = client.get("/api/videos").json()
    assert [v["id"] for v in listing["videos"]] == [video["id"]]
    assert listing["disk"]["free"] > 0

    full = client.get("/media/" + video["id"])
    assert full.status_code == 200
    assert full.content == PAYLOAD
    assert full.headers["content-type"] == "video/mp4"

    assert client.delete("/api/videos/" + video["id"]).status_code == 204
    assert client.get("/api/videos").json()["videos"] == []
    assert client.get("/media/" + video["id"]).status_code == 404
    assert list((tmp_path / "videos").iterdir()) == []
    assert list((tmp_path / "uploads").iterdir()) == []


def test_range_request(client):
    video = upload(client).json()
    r = client.get("/media/" + video["id"], headers={"Range": "bytes=100-199"})
    assert r.status_code == 206
    assert r.content == PAYLOAD[100:200]
    assert r.headers["content-range"] == "bytes 100-199/%d" % len(PAYLOAD)


def test_file_on_disk_uses_server_generated_name(client, tmp_path):
    r = upload(client, name="..%2F..%2Fevil.mp4")
    assert r.status_code == 201
    video = r.json()
    assert video["name"] == "evil.mp4"
    files = [p.name for p in (tmp_path / "videos").iterdir()]
    assert files == [video["id"] + ".mp4"]


def test_cyrillic_name(client):
    r = upload(client, name="%D0%9C%D0%BE%D1%80%D0%B5.mov")
    assert r.status_code == 201
    assert r.json()["name"] == "Море.mov"


def test_rejects_unsupported_type(client):
    assert upload(client, name="notes.txt").status_code == 415


def test_rejects_empty_file(client):
    assert upload(client, data=b"").status_code == 400


def test_rejects_too_large_file(tmp_path):
    with make_client(tmp_path, max_upload_bytes=1000) as c:
        assert upload(c).status_code == 413
        assert list((tmp_path / "uploads").iterdir()) == []


def test_rejects_when_disk_is_full(tmp_path):
    settings = Settings(data_dir=tmp_path, disk_reserve_bytes=10**18)
    with TestClient(create_app(settings)) as c:
        assert upload(c).status_code == 507


def test_delete_unknown_video(client):
    assert client.delete("/api/videos/nope").status_code == 404


def test_list_survives_restart_and_parts_are_cleaned(tmp_path):
    with make_client(tmp_path) as c:
        video = upload(c).json()
    (tmp_path / "uploads" / "leftover.part").write_bytes(b"x")

    with make_client(tmp_path) as c:
        assert [v["id"] for v in c.get("/api/videos").json()["videos"]] == [video["id"]]
        assert not (tmp_path / "uploads" / "leftover.part").exists()
