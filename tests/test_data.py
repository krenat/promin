"""The data file: what survives a restart, a damaged file, and the move from JSON files."""
from __future__ import annotations

import json
import sqlite3

import pytest

from helpers import (
    NEW_ENCODING,
    VIEWER_PASSWORD,
    add_fake_video,
    add_user,
    another,
    make_client,
    sign_in,
    video_ids,
)
from promin.db import DB_NAME, Database
from promin.encoding import Encoding


def test_a_transaction_stores_everything_or_nothing(tmp_path):
    db = Database(tmp_path / DB_NAME)
    db.open()
    with pytest.raises(RuntimeError):
        with db.transaction():
            db.run("INSERT INTO folders (id, name, created) VALUES ('a', 'A', 1)")
            with db.transaction():  # joins the one around it
                db.run("INSERT INTO folders (id, name, created) VALUES ('b', 'B', 1)")
            raise RuntimeError("the power went out")
    assert db.all("SELECT * FROM folders") == []
    with db.transaction():
        db.run("INSERT INTO folders (id, name, created) VALUES ('a', 'A', 1)")
    db.close()

    again = Database(tmp_path / DB_NAME)
    again.open()
    assert [row["id"] for row in again.all("SELECT id FROM folders")] == ["a"]
    again.close()


def test_data_file_is_private(tmp_path):
    with make_client(tmp_path) as c:
        assert add_user(c).status_code == 201
        # The journal next to it too, while the service runs: the hashes pass through it.
        names = [path.name for path in tmp_path.glob(DB_NAME + "*")]
        assert DB_NAME + "-wal" in names
        for name in names:
            assert (tmp_path / name).stat().st_mode & 0o077 == 0, name
    for name in (DB_NAME, DB_NAME + ".bak"):
        assert (tmp_path / name).stat().st_mode & 0o077 == 0, name


def test_a_database_in_trouble_but_not_damaged_is_left_where_it_is(tmp_path, monkeypatch):
    """Locked by another process, a full disk: the service does not start, and the file is
    neither moved aside nor replaced by the copy."""
    with make_client(tmp_path) as c:
        add_fake_video(c, tmp_path, "aaa")
    before = (tmp_path / DB_NAME).read_bytes()

    def locked(conn):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(Database, "_migrate", staticmethod(locked))
    with pytest.raises(sqlite3.OperationalError):
        Database(tmp_path / DB_NAME).open()
    assert (tmp_path / DB_NAME).read_bytes() == before
    assert not list(tmp_path.glob(DB_NAME + "*.broken-*"))


def test_when_the_copy_is_damaged_too_both_are_kept_aside(tmp_path):
    with make_client(tmp_path) as c:
        add_fake_video(c, tmp_path, "aaa")
    (tmp_path / DB_NAME).write_bytes(b"the database, damaged")
    (tmp_path / (DB_NAME + ".bak")).write_bytes(b"the copy, damaged")
    with make_client(tmp_path) as c:
        assert video_ids(c) == set()
    # A journal that was lying next to the database goes aside with it; it is not looked at here.
    kept = sorted(
        path.read_bytes() for path in tmp_path.glob(DB_NAME + "*.broken-*")
        if not path.name.endswith(("-wal", "-shm"))
    )
    assert kept == [b"the copy, damaged", b"the database, damaged"]


def test_damaged_data_file_falls_back_to_the_copy_of_the_last_start(tmp_path):
    with make_client(tmp_path) as c:
        add_fake_video(c, tmp_path, "aaa")
    with make_client(tmp_path) as c:  # this start keeps a copy that has "aaa"
        add_fake_video(c, tmp_path, "bbb")
    (tmp_path / DB_NAME).write_bytes(b"garbage, not a database")
    for leftover in tmp_path.glob(DB_NAME + "-*"):
        leftover.unlink()
    with make_client(tmp_path) as c:
        assert video_ids(c) == {"aaa"}
    # Kept aside for a person to look at, not overwritten.
    assert list(tmp_path.glob(DB_NAME + ".broken-*"))


def test_damaged_data_file_without_a_copy_does_not_stop_the_service(tmp_path):
    (tmp_path / DB_NAME).write_bytes(b"garbage, not a database")
    with make_client(tmp_path) as c:
        assert c.get("/api/me").json()["username"] == "admin"
        assert video_ids(c) == set()
    assert list(tmp_path.glob(DB_NAME + ".broken-*"))


def test_data_of_a_newer_version_is_not_touched(tmp_path):
    conn = sqlite3.connect(str(tmp_path / DB_NAME))
    conn.execute("PRAGMA user_version=999")
    conn.close()
    with pytest.raises(RuntimeError):
        with make_client(tmp_path, login=False):
            pass
    assert not list(tmp_path.glob(DB_NAME + ".broken-*"))


# ------------------------------------------------------ the move from JSON files


def old_files(tmp_path, **files) -> None:
    for name, content in files.items():
        text = content if isinstance(content, str) else json.dumps(content)
        (tmp_path / (name + ".json")).write_text(text, encoding="utf-8")


OLD_VIDEO = {
    "id": "aaa", "name": "a.mp4", "size": 5, "status": "ready", "created": 10.0,
    "source": "aaa.mp4", "duration": 1.5, "thumb": True, "thumb_v": 2, "error": None,
    "folder": "f1", "info": {"width": 320, "height": 240},
}


def test_json_files_of_an_older_version_are_brought_over(tmp_path):

    from promin import crypto
    from promin.crypto import hash_password, hash_secret

    salt = bytes(16)
    user = {
        "role": "viewer", "salt": salt.hex(), "created": 5.0,
        "hash": hash_password(VIEWER_PASSWORD, salt, crypto.LEGACY_PBKDF2_ROUNDS),
    }
    old_files(
        tmp_path,
        videos={"aaa": OLD_VIDEO, "bbb": dict(OLD_VIDEO, id="bbb", created=20.0, folder=None)},
        folders={"f1": {"name": "Фільми", "created": 1.0}},
        queue=[{"entry": "e1", "video_id": "bbb"}, {"entry": "e2", "video_id": "aaa"}],
        users={"olena": user},
        devices={"d1": {"name": "Проектор", "hash": hash_secret("s"), "approved_by": "admin",
                        "created": 9e12}},
        encoding=NEW_ENCODING,
    )
    for folder in ("videos", "thumbs"):
        (tmp_path / folder).mkdir()

    with make_client(tmp_path) as c:
        data = c.get("/api/videos").json()
        assert [(v["id"], v["folder"]) for v in data["videos"]] == [("bbb", None), ("aaa", "f1")]
        assert data["videos"][1]["info"] == {"width": 320, "height": 240}
        assert data["folders"] == [{"id": "f1", "name": "Фільми", "count": 1}]
        assert [e["video_id"] for e in c.get("/api/queue").json()["queue"]] == ["bbb", "aaa"]
        assert c.get("/api/settings").json()["encoding"] == NEW_ENCODING
        # A projector paired before `seen` was recorded counts from its pairing.
        assert [d["seen"] for d in c.get("/api/devices").json()["devices"]] == [9e12]
        # The old password still logs in, and is hashed again with today's rounds.
        users = c.app.state.services.users
        assert users.get("olena")["rounds"] == crypto.LEGACY_PBKDF2_ROUNDS
        phone = another(c)
        assert sign_in(phone, "olena", VIEWER_PASSWORD).status_code == 303
        assert users.get("olena")["rounds"] == crypto.PBKDF2_ROUNDS

    # The files stay as a copy and are not read again: what changed since is not undone.
    assert not (tmp_path / "videos.json").exists()
    assert json.loads((tmp_path / "videos.json.imported").read_text(encoding="utf-8"))["aaa"]
    with make_client(tmp_path) as c:
        c.delete("/api/videos/aaa")
    old_files(tmp_path, videos={"zzz": dict(OLD_VIDEO, id="zzz")})
    with make_client(tmp_path) as c:
        assert video_ids(c) == {"bbb"}


def test_sessions_of_an_older_version_stay_logged_in(tmp_path):
    from helpers import TOKEN
    from promin.auth import Auth
    from promin.crypto import hash_secret

    auth = Auth(TOKEN, None, None, None)  # only the fingerprint of the built-in admin is needed
    old_files(tmp_path, sessions={"s1": {
        "username": "admin", "hash": hash_secret("secret"),
        "fingerprint": auth.fingerprint("admin"), "created": 1.0, "seen": 9e12,
    }})
    with make_client(tmp_path, login=False) as c:
        c.cookies.set("promin_session", "s1.secret")
        assert c.get("/api/me").json()["username"] == "admin"


def test_half_written_json_file_uses_its_previous_version(tmp_path):
    # A power cut while the old version was writing: the file is cut short, `.bak` is whole.
    (tmp_path / "videos.json").write_text('{"aaa": {"id": "aaa", "na', encoding="utf-8")
    (tmp_path / "videos.json.bak").write_text(json.dumps({"aaa": OLD_VIDEO}), encoding="utf-8")
    with make_client(tmp_path) as c:
        assert video_ids(c) == {"aaa"}
    assert list(tmp_path.glob("videos.json.broken-*"))


@pytest.mark.parametrize(
    "name, content",
    [
        ("videos", "garbage"),
        ("videos", {"aaa": {"id": "aaa", "name": "a.mp4"}}),
        ("videos", {"aaa": {"id": "aaa", "name": "a.mp4", "status": "ready", "created": "today"}}),
        ("users", {"olena": {"role": "viewer"}}),
        ("sessions", {"abc": {"username": "admin"}}),
        ("devices", {"abc": {"name": "Проектор"}}),
        ("folders", [1, 2]),
        ("queue", [{"oops": 1}]),
        ("encoding", "not json"),
    ],
)
def test_damaged_json_file_is_left_out_and_the_rest_is_brought_over(tmp_path, name, content):
    files = {"folders": {"f1": {"name": "Фільми", "created": 1.0}}, "videos": {"aaa": OLD_VIDEO}}
    files[name] = content
    old_files(tmp_path, **files)
    with make_client(tmp_path) as c:
        data = c.get("/api/videos").json()
        assert [v["id"] for v in data["videos"]] == ([] if name == "videos" else ["aaa"])
        assert [f["id"] for f in data["folders"]] == ([] if name == "folders" else ["f1"])
        assert c.get("/api/users").status_code == 200
        assert c.get("/api/devices").json()["devices"] == []
        assert c.get("/api/queue").json() == {"queue": []}
        assert c.get("/api/settings").json()["encoding"]["max_height"] == 1080
        if name == "folders":
            # The video reads as having no folder, but keeps its folder id.
            assert data["videos"][0]["folder"] is None
            assert c.app.state.services.videos.get("aaa")["folder"] == "f1"
    # Kept aside for a person to look at, not overwritten.
    assert list(tmp_path.glob(name + ".json.broken-*"))


@pytest.mark.parametrize("stored", [{"max_height": 999}, {"crf": "21"}, {"max_bitrate_kbps": None}, [1]])
def test_stored_settings_that_no_longer_fit_fall_back_to_defaults(tmp_path, stored):
    old_files(tmp_path, encoding=stored)
    with make_client(tmp_path) as c:
        assert c.get("/api/settings").json()["encoding"] == Encoding().__dict__


def test_a_first_start_that_was_cut_short_still_brings_the_files_over(tmp_path):
    """The database is there, made a moment before the power went, and the JSON files were
    not read yet: the next start reads them."""
    db = Database(tmp_path / DB_NAME)
    db.open()
    db.close()
    old_files(tmp_path, videos={"aaa": OLD_VIDEO})
    with make_client(tmp_path) as c:
        assert video_ids(c) == {"aaa"}


def test_old_copies_of_the_json_files_never_come_back(tmp_path):
    old_files(tmp_path, videos={"aaa": OLD_VIDEO})
    (tmp_path / "videos.json.bak").write_text(json.dumps({"old": dict(OLD_VIDEO, id="old")}), encoding="utf-8")
    with make_client(tmp_path) as c:
        assert video_ids(c) == {"aaa"}
    assert not (tmp_path / "videos.json.bak").exists()
    # Even a database that had to be started over does not read them.
    (tmp_path / DB_NAME).write_bytes(b"damaged")
    (tmp_path / (DB_NAME + ".bak")).write_bytes(b"damaged")
    with make_client(tmp_path) as c:
        assert video_ids(c) == set()


def test_users_and_their_passwords_survive_restart(tmp_path):
    with make_client(tmp_path) as c:
        assert add_user(c).status_code == 201
    assert VIEWER_PASSWORD.encode() not in (tmp_path / DB_NAME).read_bytes()
    with make_client(tmp_path, login=False) as c:
        c.follow_redirects = False
        assert sign_in(c, "olena", VIEWER_PASSWORD).status_code == 303
