"""Folders of the library."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from helpers import add_fake_video, make_client, pair_key, pair_status, start_pairing


def make_folder(client: TestClient, name: str) -> str:
    r = client.post("/api/folders", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def library(client: TestClient) -> dict:
    return client.get("/api/videos").json()


def test_folders_are_created_renamed_and_listed(client):
    zoo = make_folder(client, "  Мультики  ")
    make_folder(client, "abc")
    # Sorted by name, case aside; spaces tidied.
    assert [f["name"] for f in library(client)["folders"]] == ["abc", "Мультики"]
    assert client.patch("/api/folders/" + zoo, json={"name": "Кіно"}).json() == {"id": zoo, "name": "Кіно"}
    assert [f["name"] for f in library(client)["folders"]] == ["abc", "Кіно"]
    assert client.patch("/api/folders/nope", json={"name": "x"}).status_code == 404


@pytest.mark.parametrize("name, status", [("", 400), ("   ", 400), ("x" * 61, 400), ("ABC", 409)])
def test_bad_folder_names_are_rejected(client, name, status):
    make_folder(client, "abc")
    assert client.post("/api/folders", json={"name": name}).status_code == status
    assert len(library(client)["folders"]) == 1


def test_renaming_to_a_taken_name_is_rejected(client):
    make_folder(client, "abc")
    other = make_folder(client, "def")
    assert client.patch("/api/folders/" + other, json={"name": "Abc"}).status_code == 409
    # Changing only the case of its own name is fine.
    assert client.patch("/api/folders/" + other, json={"name": "DEF"}).status_code == 200


def test_videos_move_between_folders(client, tmp_path):
    for video_id in ("aaa", "bbb"):
        add_fake_video(client, tmp_path, video_id)
    films = make_folder(client, "Фільми")
    r = client.post("/api/videos/aaa/folder", json={"folder": films})
    assert r.status_code == 200 and r.json()["folder"] == films
    data = library(client)
    assert {v["id"]: v["folder"] for v in data["videos"]} == {"aaa": films, "bbb": None}
    assert data["folders"] == [{"id": films, "name": "Фільми", "count": 1}]

    assert client.post("/api/videos/aaa/folder", json={"folder": None}).json()["folder"] is None
    assert client.post("/api/videos/aaa/folder", json={"folder": "nope"}).status_code == 404
    assert client.post("/api/videos/nope/folder", json={"folder": films}).status_code == 404


def test_deleting_a_folder_keeps_its_videos(client, tmp_path):
    add_fake_video(client, tmp_path, "aaa")
    films = make_folder(client, "Фільми")
    client.post("/api/videos/aaa/folder", json={"folder": films})
    assert client.delete("/api/folders/" + films).status_code == 204
    assert client.delete("/api/folders/" + films).status_code == 404
    data = library(client)
    assert data["folders"] == []
    assert [(v["id"], v["folder"]) for v in data["videos"]] == [("aaa", None)]
    stored = json.loads((tmp_path / "videos.json").read_text(encoding="utf-8"))
    assert stored["aaa"]["folder"] is None


def test_upload_goes_into_the_folder_being_looked_at(client):
    films = make_folder(client, "Фільми")
    r = client.post("/api/videos", content=b"x" * 100, headers={"X-Filename": "a.mp4", "X-Folder": films})
    assert r.status_code == 201 and r.json()["folder"] == films
    # An unknown folder (deleted meanwhile) means no folder rather than a failed upload.
    r = client.post("/api/videos", content=b"x" * 100, headers={"X-Filename": "b.mp4", "X-Folder": "gone"})
    assert r.status_code == 201 and r.json()["folder"] is None


def test_folders_survive_restart(tmp_path):
    with make_client(tmp_path) as c:
        add_fake_video(c, tmp_path, "aaa")
        films = make_folder(c, "Фільми")
        c.post("/api/videos/aaa/folder", json={"folder": films})
    with make_client(tmp_path) as c:
        data = library(c)
        assert data["folders"] == [{"id": films, "name": "Фільми", "count": 1}]
        assert data["videos"][0]["folder"] == films


def test_damaged_folders_file_does_not_stop_the_service(tmp_path):
    with make_client(tmp_path) as c:
        add_fake_video(c, tmp_path, "aaa")
        films = make_folder(c, "Фільми")
        c.post("/api/videos/aaa/folder", json={"folder": films})
    (tmp_path / "folders.json").write_text("[1, 2]", encoding="utf-8")
    with make_client(tmp_path) as c:
        data = library(c)
        # The video reads as having no folder, but keeps its folder id for when the file is fixed.
        assert data["folders"] == [] and data["videos"][0]["folder"] is None
    stored = json.loads((tmp_path / "videos.json").read_text(encoding="utf-8"))
    assert stored["aaa"]["folder"] == films


def test_folder_limit(client, monkeypatch):
    from promin import library as library_module
    monkeypatch.setattr(library_module, "FOLDERS_MAX", 2)
    make_folder(client, "a")
    make_folder(client, "b")
    assert client.post("/api/folders", json={"name": "c"}).status_code == 409


def test_viewer_manages_folders_but_a_projector_does_not(client, viewer, projector, tmp_path):
    add_fake_video(client, tmp_path, "aaa")
    films = make_folder(viewer, "Фільми")
    assert viewer.post("/api/videos/aaa/folder", json={"folder": films}).status_code == 200
    assert viewer.patch("/api/folders/" + films, json={"name": "Кіно"}).status_code == 200

    pairing = start_pairing(projector)
    client.post("/api/pair/approve", json=pair_key(pairing))
    pair_status(projector, pairing["token"])
    assert projector.post("/api/folders", json={"name": "x"}).status_code == 403
    assert projector.post("/api/videos/aaa/folder", json={"folder": None}).status_code == 403
    assert projector.delete("/api/folders/" + films).status_code == 403
    assert viewer.delete("/api/folders/" + films).status_code == 204


def test_deleting_a_folder_writes_the_list_once(client, tmp_path, monkeypatch):
    from promin import library as library_module

    folder = make_folder(client, "Мультики")
    for i in range(10):
        add_fake_video(client, tmp_path, "v%02d" % i)
        client.post("/api/videos/v%02d/folder" % i, json={"folder": folder})
    writes = []
    real = library_module.write_json
    monkeypatch.setattr(library_module, "write_json", lambda path, *a, **kw: (writes.append(path.name), real(path, *a, **kw)))
    assert client.delete("/api/folders/" + folder).status_code == 204
    assert writes.count("videos.json") == 1
    assert all(v["folder"] is None for v in library(client)["videos"])
