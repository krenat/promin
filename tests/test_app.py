from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main import (  # noqa: E402
    HDR_FILTER_ZSCALE,
    Encoding,
    ProcessingError,
    Settings,
    build_ffmpeg_args,
    clean_tvtest_report,
    create_app,
    plan_encode,
    read_json,
    thumbnail_args,
    tvtest_fits,
    tvtest_levels,
    video_info,
    write_json,
)

TOKEN = "test-token-1234567890"
FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="needs ffmpeg")


def make_clip(path: Path, size: str = "320x240", extra: tuple = ()) -> bytes:
    subprocess.run(
        [
            FFMPEG, "-v", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=%s:rate=30" % size,
            "-f", "lavfi", "-i", "sine=frequency=440",
            "-t", "1", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", *extra, str(path),
        ],
        check=True,
    )
    return path.read_bytes()


@pytest.fixture(scope="session")
def small_clip(tmp_path_factory) -> bytes:
    return make_clip(tmp_path_factory.mktemp("clips") / "small.mp4")


@pytest.fixture(scope="session")
def big_clip(tmp_path_factory) -> bytes:
    return make_clip(tmp_path_factory.mktemp("clips") / "big.mp4", size="2560x1440")


def make_client(tmp_path: Path, login: bool = True, **overrides) -> TestClient:
    overrides.setdefault("disk_reserve_bytes", 0)
    settings = Settings(data_dir=tmp_path, token=TOKEN, **overrides)
    client = TestClient(create_app(settings))
    if login:
        # Sessions live in the data directory, so the cookie also works in an app started later.
        with TestClient(create_app(settings), follow_redirects=False) as tmp:
            r = sign_in(tmp)
            assert r.status_code == 303
            client.cookies.set("promin_session", r.cookies["promin_session"])
    return client


def sign_in(client: TestClient, username: str = "admin", password: str = TOKEN):
    return client.post("/login", data={"username": username, "password": password})


@pytest.fixture
def client(tmp_path: Path):
    with make_client(tmp_path) as c:
        yield c


def upload(client: TestClient, data: bytes, name: str = "clip.mp4"):
    return client.post("/api/videos", content=data, headers={"X-Filename": name})


def wait_done(client: TestClient, video_id: str, timeout: float = 60) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        videos = {v["id"]: v for v in client.get("/api/videos").json()["videos"]}
        if videos[video_id]["status"] in ("ready", "error"):
            return videos[video_id]
        time.sleep(0.1)
    raise AssertionError("processing did not finish in %s s" % timeout)


def probe_video(path: Path) -> dict:
    out = subprocess.run(
        [
            shutil.which("ffprobe"), "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,pix_fmt,width,height",
            "-of", "json", str(path),
        ],
        check=True, capture_output=True, text=True,
    ).stdout
    return json.loads(out)["streams"][0]


# -------------------------------------------------------- decisions from ffprobe


def fake_probe(**video) -> dict:
    stream = {
        "codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p",
        "width": 1920, "height": 1080, "avg_frame_rate": "30/1", "bit_rate": "6000000",
    }
    stream.update(video)
    return {
        "streams": [stream, {"codec_type": "audio", "codec_name": "aac"}],
        "format": {"duration": "12.5"},
    }


SETTINGS = Encoding()


def test_plain_h264_is_remuxed():
    plan = plan_encode(fake_probe(), SETTINGS)
    assert plan["mode"] == "remux"
    assert plan["duration"] == 12.5


@pytest.mark.parametrize(
    "video",
    [
        {"codec_name": "hevc"},
        {"pix_fmt": "yuv420p10le"},
        {"color_transfer": "smpte2084"},
        {"color_transfer": "arib-std-b67"},
        {"width": 3840, "height": 2160},
        {"width": 1080, "height": 1920},
        {"avg_frame_rate": "120/1"},
        {"bit_rate": "40000000"},
        {"side_data_list": [{"rotation": -90}]},
        {"tags": {"rotate": "90"}},
    ],
)
def test_everything_else_is_transcoded(video):
    assert plan_encode(fake_probe(**video), SETTINGS)["mode"] == "transcode"


def test_only_audio_is_transcoded_when_video_already_fits():
    probe = fake_probe()
    probe["streams"][1]["codec_name"] = "ac3"
    assert plan_encode(probe, SETTINGS)["mode"] == "audio"
    # AAC 5.1 is downmixed to stereo too, without touching the picture.
    surround = fake_probe()
    surround["streams"][1]["channels"] = 6
    assert plan_encode(surround, SETTINGS)["mode"] == "audio"


def test_unfit_video_is_fully_transcoded_whatever_the_audio():
    probe = fake_probe(codec_name="hevc")
    probe["streams"][1]["codec_name"] = "ac3"
    assert plan_encode(probe, SETTINGS)["mode"] == "transcode"


def test_audio_only_args_copy_video():
    probe = fake_probe()
    probe["streams"][1]["codec_name"] = "ac3"
    plan = plan_encode(probe, SETTINGS)
    args = build_ffmpeg_args("ffmpeg", Path("in.mkv"), Path("out.mp4"), plan, SETTINGS, None)
    assert args[args.index("-c:v") + 1] == "copy"
    assert args[args.index("-c:a") + 1] == "aac"
    assert args[args.index("-ac") + 1] == "2"
    assert "-vf" not in args and "libx264" not in args
    assert "+faststart" in args


def test_file_without_video_is_rejected():
    with pytest.raises(ProcessingError):
        plan_encode({"streams": [{"codec_type": "audio", "codec_name": "aac"}]}, SETTINGS)


def test_transcode_args_follow_the_plan():
    plan = plan_encode(fake_probe(color_transfer="smpte2084", avg_frame_rate="240/1"), SETTINGS)
    args = build_ffmpeg_args("ffmpeg", Path("in.mov"), Path("out.mp4"), plan, SETTINGS, HDR_FILTER_ZSCALE)
    filters = args[args.index("-vf") + 1]
    assert filters.startswith("scale=")
    assert filters.index("scale=") < filters.index(HDR_FILTER_ZSCALE)
    assert "fps=60" in filters
    assert filters.endswith("format=yuv420p")
    assert args[args.index("-maxrate") + 1] == "6000k"  # the source bitrate
    assert "-force_key_frames" in args
    assert args[args.index("-color_trc") + 1] == "bt709"


def test_remux_args_copy_streams():
    plan = plan_encode(fake_probe(), SETTINGS)
    args = build_ffmpeg_args("ffmpeg", Path("in.mp4"), Path("out.mp4"), plan, SETTINGS, None)
    assert args[args.index("-c") + 1] == "copy"
    assert "-vf" not in args
    assert "+faststart" in args


# ------------------------------------------------------------ the whole service


def test_index_page(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Promin" in r.text


@needs_ffmpeg
def test_upload_process_play_delete(client, tmp_path, small_clip):
    r = upload(client, small_clip, name="%D0%9C%D0%BE%D1%80%D0%B5.mov")
    assert r.status_code == 201
    created = r.json()
    assert created["name"] == "Море.mov"
    assert created["status"] == "queued"

    video = wait_done(client, created["id"])
    assert video["status"] == "ready", video["error"]
    assert video["thumb"] is True
    assert 0.9 < video["duration"] < 1.2

    # The original is gone; the finished file is named by the server.
    assert list((tmp_path / "uploads").iterdir()) == []
    ready = tmp_path / "videos" / (video["id"] + ".mp4")
    assert [p.name for p in (tmp_path / "videos").iterdir()] == [ready.name]
    data = ready.read_bytes()

    full = client.get("/media/" + video["id"])
    assert full.status_code == 200
    assert full.headers["content-type"] == "video/mp4"
    assert full.content == data

    part = client.get("/media/" + video["id"], headers={"Range": "bytes=100-199"})
    assert part.status_code == 206
    assert part.content == data[100:200]
    assert part.headers["content-range"] == "bytes 100-199/%d" % len(data)

    thumb = client.get("/thumb/" + video["id"])
    assert thumb.status_code == 200
    assert thumb.content[:2] == b"\xff\xd8"  # JPEG

    assert client.delete("/api/videos/" + video["id"]).status_code == 204
    assert client.get("/api/videos").json()["videos"] == []
    assert client.get("/media/" + video["id"]).status_code == 404
    assert client.get("/thumb/" + video["id"]).status_code == 404
    for folder in ("uploads", "videos", "thumbs"):
        assert list((tmp_path / folder).iterdir()) == []


@needs_ffmpeg
def test_large_video_is_scaled_down(client, tmp_path, big_clip):
    created = upload(client, big_clip).json()
    video = wait_done(client, created["id"])
    assert video["status"] == "ready", video["error"]
    info = probe_video(tmp_path / "videos" / (video["id"] + ".mp4"))
    assert info["codec_name"] == "h264"
    assert info["pix_fmt"] == "yuv420p"
    assert (info["width"], info["height"]) == (1920, 1080)


@needs_ffmpeg
def test_broken_file_gets_error_and_can_be_retried(client, tmp_path):
    created = upload(client, b"this is not a video" * 100).json()
    video = wait_done(client, created["id"])
    assert video["status"] == "error"
    assert video["error"]
    assert client.get("/media/" + video["id"]).status_code == 404
    # The original stays so that processing can be retried.
    assert len(list((tmp_path / "uploads").iterdir())) == 1

    assert client.post("/api/videos/%s/retry" % video["id"]).status_code == 200
    assert wait_done(client, video["id"])["status"] == "error"


@needs_ffmpeg
def test_retry_is_only_for_failed_videos(client, small_clip):
    created = upload(client, small_clip).json()
    wait_done(client, created["id"])
    assert client.post("/api/videos/%s/retry" % created["id"]).status_code == 409
    assert client.post("/api/videos/nope/retry").status_code == 404


@needs_ffmpeg
def test_delete_while_waiting_in_queue(client, tmp_path, big_clip, small_clip):
    first = upload(client, big_clip).json()
    second = upload(client, small_clip).json()
    assert client.delete("/api/videos/" + second["id"]).status_code == 204
    assert client.delete("/api/videos/" + first["id"]).status_code == 204
    time.sleep(1.5)  # the worker has to notice the deletion and clean up
    assert client.get("/api/videos").json()["videos"] == []
    for folder in ("uploads", "videos", "thumbs"):
        assert list((tmp_path / folder).iterdir()) == []


@needs_ffmpeg
def test_unfinished_jobs_resume_after_restart(tmp_path, small_clip):
    (tmp_path / "uploads").mkdir()
    (tmp_path / "videos").mkdir()
    (tmp_path / "uploads" / "abc.mp4").write_bytes(small_clip)
    (tmp_path / "uploads" / "leftover.part").write_bytes(b"x")
    (tmp_path / "videos" / "abc.tmp.mp4").write_bytes(b"half-written")
    item = {
        "id": "abc", "name": "clip.mp4", "size": len(small_clip), "status": "processing",
        "created": 1.0, "source": "abc.mp4", "duration": None, "thumb": False, "error": None,
    }
    (tmp_path / "videos.json").write_text(json.dumps({"abc": item}), encoding="utf-8")

    with make_client(tmp_path) as c:
        assert not (tmp_path / "uploads" / "leftover.part").exists()
        assert wait_done(c, "abc")["status"] == "ready"
        assert not (tmp_path / "videos" / "abc.tmp.mp4").exists()


def test_file_name_cannot_escape_data_dir(client, tmp_path):
    r = upload(client, b"x" * 100, name="..%2F..%2Fevil.mp4")
    assert r.status_code == 201
    assert r.json()["name"] == "evil.mp4"
    stored = [p.name for p in (tmp_path / "uploads").iterdir()]
    assert all(name.startswith(r.json()["id"]) for name in stored)


def test_rejects_unsupported_type(client):
    assert upload(client, b"hello", name="notes.txt").status_code == 415


def test_rejects_empty_file(client):
    assert upload(client, b"").status_code == 400


def test_rejects_too_large_file(tmp_path):
    with make_client(tmp_path, max_upload_bytes=1000) as c:
        assert upload(c, b"x" * 2000).status_code == 413
        assert list((tmp_path / "uploads").iterdir()) == []


def test_rejects_when_disk_is_full(tmp_path):
    with make_client(tmp_path, disk_reserve_bytes=10**18) as c:
        assert upload(c, b"x" * 100).status_code == 507


def test_delete_unknown_video(client):
    assert client.delete("/api/videos/nope").status_code == 404


# ------------------------------------------------------------------------- login


@pytest.fixture
def anon(tmp_path: Path):
    with make_client(tmp_path, login=False) as c:
        c.follow_redirects = False
        yield c


def test_app_refuses_to_start_without_token(tmp_path):
    with pytest.raises(RuntimeError):
        create_app(Settings(data_dir=tmp_path, token=""))
    with pytest.raises(RuntimeError):
        create_app(Settings(data_dir=tmp_path, token="short"))


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
    anon = TestClient(client.app, follow_redirects=False)
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
    original = anon.app.state.users.get

    def spy(username):
        # At this moment the password is still being checked, yet the attempt is recorded.
        limiter_hits.append(sign_in_blocked_after(anon, 4))
        return original(username)

    anon.app.state.users.get = spy
    assert sign_in(anon, "nobody", "whatever-123").status_code == 401
    assert limiter_hits == [True]


def sign_in_blocked_after(client: TestClient, more: int) -> bool:
    """Whether login is blocked after `more` further failed attempts."""
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
    # The file holds only a hash of the secret.
    stored = (tmp_path / "sessions.json").read_text(encoding="utf-8")
    assert first.split(".", 1)[1] not in stored and TOKEN not in stored


def test_logout_revokes_the_session_on_the_server(anon):
    cookie = sign_in(anon).cookies["promin_session"]
    assert anon.post("/logout").status_code == 303
    # Even if someone kept the cookie, it no longer works.
    anon.cookies.set("promin_session", cookie)
    assert anon.get("/api/me").status_code == 401


def test_logout_needs_the_real_cookie_to_revoke(anon):
    cookie = sign_in(anon).cookies["promin_session"]
    other = TestClient(anon.app, follow_redirects=False)
    other.cookies.set("promin_session", cookie.split(".", 1)[0] + ".guess")
    assert other.post("/logout").status_code == 303
    assert anon.get("/api/me").status_code == 200


def test_idle_session_expires(anon):
    cookie = sign_in(anon).cookies["promin_session"]
    sessions = anon.app.state.sessions
    session_id = cookie.split(".", 1)[0]
    # The session was used: the stamp is refreshed and it lives on.
    sessions._sessions[session_id]["seen"] -= 29 * 24 * 3600
    assert anon.get("/api/me").status_code == 200
    assert time.time() - sessions.get(session_id)["seen"] < 60
    # Unused for more than 30 days: the session is gone.
    sessions._sessions[session_id]["seen"] -= 31 * 24 * 3600
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


# ------------------------------------------------------------- users and roles

VIEWER_PASSWORD = "viewer-pass-1"


def add_user(client: TestClient, username: str = "olena", password: str = VIEWER_PASSWORD, role: str = "viewer"):
    return client.post("/api/users", json={"username": username, "password": password, "role": role})


@pytest.fixture
def viewer(client, tmp_path):
    """A client logged in as a viewer; `client` alongside stays the administrator."""
    assert add_user(client).status_code == 201
    c = TestClient(client.app, follow_redirects=False)
    assert sign_in(c, "olena", VIEWER_PASSWORD).status_code == 303
    return c


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


# ----------------------------------------------------------------------- remote

from starlette.websockets import WebSocketDisconnect  # noqa: E402


def tv_state(**fields) -> dict:
    state = {
        "type": "state", "video_id": None, "name": None, "position": 0, "duration": 0,
        "paused": True, "volume": 1, "started": True,
    }
    state.update(fields)
    return state


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


# --------------------------------------------------- pairing a projector by QR code


def start_pairing(tv: TestClient) -> dict:
    r = tv.post("/api/pair/start")
    assert r.status_code == 200
    return r.json()


def pair_key(pairing: dict) -> dict:
    """What the phone sends after scanning: the key from the address in the QR code."""
    return {"key": pairing["url"].split("?t=")[1]}


def pair_status(tv: TestClient, token: str) -> str:
    return tv.post("/api/pair/status", json={"token": token}).json()["status"]


@pytest.fixture
def projector(client):
    """A client without a login that plays the projector browser."""
    return TestClient(client.app, follow_redirects=False)


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


@pytest.mark.parametrize("next_path", ["/pair?t=short", "/pair?t=" + "a" * 32 + "&x=1", "/pair", "//evil.example/pair?t=" + "a" * 32])
def test_login_returns_only_to_a_well_formed_code(anon, next_path):
    r = anon.post("/login", data={"username": "admin", "password": TOKEN, "next": next_path})
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_a_projector_cannot_open_the_confirmation_page(client, projector):
    pairing = start_pairing(projector)
    client.post("/api/pair/approve", json=pair_key(pairing))
    pair_status(projector, pairing["token"])
    r = projector.get("/pair?t=" + "a" * 32)
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_code_expires(client, projector):
    pairing = start_pairing(projector)
    client.app.state.pairing.pending[pairing["token"]]["created"] -= 11 * 60
    assert client.post("/api/pair/approve", json=pair_key(pairing)).status_code == 404
    assert pair_status(projector, pairing["token"]) == "expired"


def test_an_approval_nobody_collects_leaves_no_device(client, projector):
    pairing = start_pairing(projector)
    assert client.post("/api/pair/approve", json=pair_key(pairing)).status_code == 200
    # The projector page was closed before it asked: nothing is paired yet.
    assert client.get("/api/devices").json()["devices"] == []
    client.app.state.pairing.pending[pairing["token"]]["created"] -= 11 * 60
    assert pair_status(projector, pairing["token"]) == "expired"
    assert client.get("/api/devices").json()["devices"] == []


def test_an_idle_projector_is_forgotten(client, projector):
    pairing = start_pairing(projector)
    client.post("/api/pair/approve", json=pair_key(pairing))
    assert pair_status(projector, pairing["token"]) == "approved"
    devices = client.app.state.devices
    device_id = client.get("/api/devices").json()["devices"][0]["id"]
    # In touch within 30 days: the stamp is refreshed and the projector stays paired.
    devices._devices[device_id]["seen"] -= 29 * 24 * 3600
    assert projector.get("/api/me").status_code == 200
    assert time.time() - client.get("/api/devices").json()["devices"][0]["seen"] < 60
    # Silent for longer: it is gone from the list without ever coming back.
    devices._devices[device_id]["seen"] -= 31 * 24 * 3600
    assert client.get("/api/devices").json()["devices"] == []
    assert projector.get("/api/me").status_code == 401


def test_a_projector_paired_before_seen_was_recorded_counts_from_its_pairing(client, projector):
    pairing = start_pairing(projector)
    client.post("/api/pair/approve", json=pair_key(pairing))
    pair_status(projector, pairing["token"])
    devices = client.app.state.devices
    device = next(iter(devices._devices.values()))
    del device["seen"]
    assert projector.get("/api/me").status_code == 200
    device["created"] -= 31 * 24 * 3600
    assert projector.get("/api/me").status_code == 401


def test_a_failed_connection_is_logged(client, caplog):
    with client.websocket_connect("/ws?role=remote") as ws:
        ws.receive_json()
        ws.send_text("not json")
        with pytest.raises(Exception):
            ws.receive_json()
    assert "A remote connection failed" in caplog.text


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
        assert cookie.split(".", 1)[1] not in (tmp_path / "devices.json").read_text(encoding="utf-8")
    with make_client(tmp_path, login=False) as c:
        c.cookies.set("promin_session", cookie)
        assert c.get("/api/me").json()["role"] == "device"


# ------------------------------------------------------------------- thumbnails


def test_thumbnail_is_taken_away_from_the_start_of_long_videos():
    args = thumbnail_args("ffmpeg", Path("in.mp4"), Path("out.jpg"), 1600.0)
    assert args[args.index("-ss") + 1] == "160.00"
    assert args[args.index("-vf") + 1].startswith("thumbnail=")
    short = thumbnail_args("ffmpeg", Path("in.mp4"), Path("out.jpg"), 4.0)
    assert short[short.index("-ss") + 1] == "1.00"
    assert "thumbnail=" not in short[short.index("-vf") + 1]


@needs_ffmpeg
def test_long_video_gets_a_thumbnail(client, tmp_path, tmp_path_factory):
    clip = tmp_path_factory.mktemp("long") / "long.mp4"
    subprocess.run(
        [
            FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25",
            "-t", "25", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(clip),
        ],
        check=True,
    )
    video = wait_done(client, upload(client, clip.read_bytes()).json()["id"])
    assert video["status"] == "ready", video["error"]
    assert video["thumb"] is True and video["thumb_v"] == 2
    assert client.get("/thumb/" + video["id"]).content[:2] == b"\xff\xd8"
    assert not list((tmp_path / "thumbs").glob("*.tmp.jpg"))


@needs_ffmpeg
def test_old_thumbnails_are_remade_on_start(tmp_path, small_clip):
    for folder in ("uploads", "videos", "thumbs"):
        (tmp_path / folder).mkdir()
    (tmp_path / "videos" / "abc.mp4").write_bytes(small_clip)
    (tmp_path / "thumbs" / "abc.jpg").write_bytes(b"old black frame")
    item = {
        "id": "abc", "name": "clip.mp4", "size": len(small_clip), "status": "ready",
        "created": 1.0, "source": "abc.mp4", "duration": 1.0, "thumb": True, "error": None,
    }
    (tmp_path / "videos.json").write_text(json.dumps({"abc": item}), encoding="utf-8")

    with make_client(tmp_path) as c:
        deadline = time.time() + 30
        while time.time() < deadline:
            video = c.get("/api/videos").json()["videos"][0]
            if video.get("thumb_v") == 2:
                break
            time.sleep(0.1)
        assert video["thumb_v"] == 2 and video["thumb"] is True
        assert c.get("/thumb/abc").content[:2] == b"\xff\xd8"


def test_tv_page_asks_for_fullscreen_on_start(anon):
    page = anon.get("/tv").text
    assert "requestFullscreen" in page and "webkitRequestFullscreen" in page


# --------------------------------------------------------- audio-only transcoding


def stream_md5(path: Path, stream: str) -> str:
    """Checksum of a track without re-encoding: identical if the track was copied."""
    return subprocess.run(
        [FFMPEG, "-v", "error", "-i", str(path), "-map", stream, "-c", "copy", "-f", "md5", "-"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


@needs_ffmpeg
def test_surround_ac3_audio_is_converted_without_touching_video(client, tmp_path, tmp_path_factory):
    source = tmp_path_factory.mktemp("ac3") / "film.mkv"
    subprocess.run(
        [
            FFMPEG, "-v", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25",
            "-f", "lavfi", "-i", "anoisesrc=color=pink:sample_rate=48000",
            "-t", "3", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "ac3", "-ac", "6", str(source),
        ],
        check=True,
    )
    created = upload(client, source.read_bytes(), name="film.mkv").json()
    video = wait_done(client, created["id"])
    assert video["status"] == "ready", video["error"]

    ready = tmp_path / "videos" / (video["id"] + ".mp4")
    audio = json.loads(subprocess.run(
        [
            shutil.which("ffprobe"), "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=codec_name,channels", "-of", "json", str(ready),
        ],
        check=True, capture_output=True, text=True,
    ).stdout)["streams"][0]
    assert (audio["codec_name"], audio["channels"]) == ("aac", 2)
    # The video track is the same, bit for bit.
    assert stream_md5(ready, "0:v:0") == stream_md5(source, "0:v:0")


# ------------------------------------------------------------ encoding settings


def test_transcoded_bitrate_follows_the_source():
    # A low-bitrate original is not inflated to the configured ceiling.
    plan = plan_encode(fake_probe(codec_name="mpeg4", bit_rate="2000000"), SETTINGS)
    assert plan["mode"] == "transcode" and plan["max_rate_kbps"] == 2000
    # More efficient codecs get headroom, because H.264 needs more bits.
    assert plan_encode(fake_probe(codec_name="hevc", bit_rate="2000000"), SETTINGS)["max_rate_kbps"] == 3600
    # A high-bitrate original is still held to the ceiling.
    assert plan_encode(fake_probe(bit_rate="40000000"), SETTINGS)["max_rate_kbps"] == 8000
    # An unknown source bitrate leaves the ceiling as configured.
    probe = fake_probe(codec_name="hevc")
    del probe["streams"][0]["bit_rate"]
    assert plan_encode(probe, SETTINGS)["max_rate_kbps"] == 8000


def test_encoding_limits_decide_what_is_transcoded():
    uhd = fake_probe(width=3840, height=2160, bit_rate="20000000")
    assert plan_encode(uhd, SETTINGS)["mode"] == "transcode"
    roomy = Encoding(max_height=2160, max_bitrate_kbps=30000)
    assert plan_encode(uhd, roomy)["mode"] == "remux"
    # A lower limit sends even a 1080p file to transcoding.
    assert plan_encode(fake_probe(), Encoding(max_height=720))["mode"] == "transcode"
    assert plan_encode(fake_probe(avg_frame_rate="60/1"), Encoding(max_fps=30))["mode"] == "transcode"


def test_transcode_args_use_the_encoding_settings():
    encoding = Encoding(
        max_height=720, max_fps=30, max_bitrate_kbps=4000, crf=18,
        audio_bitrate_kbps=192, x264_preset="fast",
    )
    plan = plan_encode(fake_probe(avg_frame_rate="60/1", bit_rate="9000000"), encoding)
    args = build_ffmpeg_args("ffmpeg", Path("in.mp4"), Path("out.mp4"), plan, encoding, None)
    filters = args[args.index("-vf") + 1]
    assert "min(1280,iw)" in filters and "min(720,ih)" in filters and "fps=30" in filters
    assert args[args.index("-crf") + 1] == "18"
    assert args[args.index("-preset") + 1] == "fast"
    assert args[args.index("-maxrate") + 1] == "4000k"
    assert args[args.index("-bufsize") + 1] == "8000k"
    assert args[args.index("-b:a") + 1] == "192k"


def test_settings_default_to_the_environment(tmp_path):
    with make_client(tmp_path, max_bitrate_kbps=6000, x264_preset="fast") as c:
        data = c.get("/api/settings").json()
        assert data["encoding"] == data["defaults"]
        assert data["encoding"]["max_bitrate_kbps"] == 6000
        assert data["encoding"]["x264_preset"] == "fast"
        assert data["encoding"]["max_height"] == 1080 and data["encoding"]["crf"] == 21
        assert data["choices"]["max_height"] == [720, 1080, 1440, 2160]


NEW_ENCODING = {
    "max_height": 2160, "max_fps": 30, "max_bitrate_kbps": 30000, "crf": 19,
    "audio_bitrate_kbps": 192, "x264_preset": "fast",
}


def test_admin_saves_settings_and_they_survive_restart(tmp_path):
    with make_client(tmp_path) as c:
        r = c.put("/api/settings", json=NEW_ENCODING)
        assert r.status_code == 200 and r.json()["encoding"] == NEW_ENCODING
        assert c.get("/api/settings").json()["encoding"] == NEW_ENCODING
    with make_client(tmp_path) as c:
        data = c.get("/api/settings").json()
        assert data["encoding"] == NEW_ENCODING
        assert data["defaults"]["max_height"] == 1080  # the reset button still has the defaults


@pytest.mark.parametrize(
    "change",
    [
        {"max_height": 1000}, {"max_fps": 24}, {"max_bitrate_kbps": 10}, {"max_bitrate_kbps": 10**6},
        {"crf": 5}, {"crf": 40}, {"audio_bitrate_kbps": 1}, {"x264_preset": "placebo; rm -rf /"},
        {"crf": "high"},
    ],
)
def test_invalid_settings_are_rejected(client, change):
    before = client.get("/api/settings").json()["encoding"]
    assert client.put("/api/settings", json={**NEW_ENCODING, **change}).status_code in (400, 422)
    assert client.get("/api/settings").json()["encoding"] == before


def test_every_user_reaches_settings(client, viewer, anon):
    assert client.get("/settings").status_code == 200
    assert viewer.get("/settings").status_code == 200
    assert viewer.put("/api/settings", json=NEW_ENCODING).status_code == 200
    assert client.get("/api/settings").json()["encoding"] == NEW_ENCODING
    assert anon.get("/api/settings").status_code == 401
    assert anon.put("/api/settings", json=NEW_ENCODING).status_code == 401
    assert anon.get("/settings").status_code == 303


def test_damaged_settings_file_falls_back_to_defaults(tmp_path):
    (tmp_path / "encoding.json").write_text('{"max_height": 999}', encoding="utf-8")
    with make_client(tmp_path) as c:
        assert c.get("/api/settings").json()["encoding"]["max_height"] == 1080
    (tmp_path / "encoding.json").write_text("not json", encoding="utf-8")
    with make_client(tmp_path) as c:
        assert c.get("/api/settings").json()["encoding"]["max_height"] == 1080


@needs_ffmpeg
def test_saved_settings_apply_to_the_next_upload(client, tmp_path, big_clip):
    assert client.put("/api/settings", json={**NEW_ENCODING, "max_height": 720}).status_code == 200
    video = wait_done(client, upload(client, big_clip).json()["id"])
    assert video["status"] == "ready", video
    stream = probe_video(tmp_path / "videos" / (video["id"] + ".mp4"))
    assert (stream["width"], stream["height"]) == (1280, 720)


# --------------------------------------------------------------- batch delete


def add_fake_video(client: TestClient, tmp_path: Path, video_id: str, status: str = "ready") -> None:
    """Puts a video straight into the store, with files, without running ffmpeg."""
    store = client.app.state.store
    store.add({
        "id": video_id, "name": video_id + ".mp4", "size": 1, "status": status,
        "created": time.time(), "source": video_id + ".mp4", "duration": 1.0,
        "thumb": True, "error": None,
    })
    (tmp_path / "videos" / (video_id + ".mp4")).write_bytes(b"v")
    (tmp_path / "thumbs" / (video_id + ".jpg")).write_bytes(b"t")


def video_ids(client: TestClient) -> set:
    return {v["id"] for v in client.get("/api/videos").json()["videos"]}


def test_admin_deletes_several_videos_at_once(client, tmp_path):
    for video_id in ("aaa", "bbb", "ccc"):
        add_fake_video(client, tmp_path, video_id)
    r = client.post("/api/videos/delete", json={"ids": ["aaa", "ccc", "aaa"]})
    assert r.status_code == 200
    assert r.json() == {"deleted": ["aaa", "ccc"], "missing": []}
    assert video_ids(client) == {"bbb"}
    left = sorted(p.name for p in (tmp_path / "videos").iterdir()) + sorted(
        p.name for p in (tmp_path / "thumbs").iterdir()
    )
    assert left == ["bbb.mp4", "bbb.jpg"]


def test_batch_delete_reports_videos_already_gone(client, tmp_path):
    add_fake_video(client, tmp_path, "aaa")
    r = client.post("/api/videos/delete", json={"ids": ["aaa", "nope"]})
    assert r.status_code == 200
    assert r.json() == {"deleted": ["aaa"], "missing": ["nope"]}


def test_batch_delete_also_removes_videos_in_processing(client, tmp_path):
    add_fake_video(client, tmp_path, "aaa", status="queued")
    add_fake_video(client, tmp_path, "bbb", status="error")
    assert client.post("/api/videos/delete", json={"ids": ["aaa", "bbb"]}).json()["missing"] == []
    assert video_ids(client) == set()


def test_batch_delete_ids_never_reach_the_file_system(client, tmp_path):
    add_fake_video(client, tmp_path, "aaa")
    # A glob pattern is not a known id, so no file is touched.
    r = client.post("/api/videos/delete", json={"ids": ["*", "../*", "a*"]})
    assert r.json() == {"deleted": [], "missing": ["*", "../*", "a*"]}
    assert (tmp_path / "videos" / "aaa.mp4").is_file()


@pytest.mark.parametrize("payload", [{"ids": []}, {"ids": [str(i) for i in range(501)]}, {}, {"ids": "aaa"}])
def test_invalid_batch_delete_is_rejected(client, tmp_path, payload):
    add_fake_video(client, tmp_path, "aaa")
    assert client.post("/api/videos/delete", json=payload).status_code in (400, 422)
    assert video_ids(client) == {"aaa"}


def test_only_admin_deletes_in_batches(client, viewer, anon, tmp_path):
    add_fake_video(client, tmp_path, "aaa")
    assert viewer.post("/api/videos/delete", json={"ids": ["aaa"]}).status_code == 403
    assert anon.post("/api/videos/delete", json={"ids": ["aaa"]}).status_code == 401
    assert video_ids(client) == {"aaa"}
    # A viewer still deletes one video at a time.
    assert viewer.delete("/api/videos/aaa").status_code == 204


# ---------------------------------------------------------------- watch queue


def queue_names(client: TestClient) -> list:
    return [item["video_id"] for item in client.get("/api/queue").json()["queue"]]


def test_queue_is_built_and_edited(client, tmp_path):
    for video_id in ("aaa", "bbb", "ccc"):
        add_fake_video(client, tmp_path, video_id)
    assert client.get("/api/queue").json() == {"queue": []}
    for video_id in ("aaa", "bbb", "ccc", "aaa"):
        r = client.post("/api/queue", json={"video_id": video_id})
        assert r.status_code == 201
    queue = r.json()["queue"]
    # The same video may be queued twice; each entry has its own id.
    assert [q["video_id"] for q in queue] == ["aaa", "bbb", "ccc", "aaa"]
    assert len({q["entry"] for q in queue}) == 4
    assert queue[0]["name"] == "aaa.mp4" and queue[0]["thumb"] is True

    r = client.post("/api/queue/%s/move" % queue[2]["entry"], json={"position": 0})
    assert [q["video_id"] for q in r.json()["queue"]] == ["ccc", "aaa", "bbb", "aaa"]
    # A position past either end is clamped.
    client.post("/api/queue/%s/move" % queue[2]["entry"], json={"position": 99})
    assert queue_names(client) == ["aaa", "bbb", "aaa", "ccc"]

    r = client.delete("/api/queue/" + queue[0]["entry"])
    assert [q["video_id"] for q in r.json()["queue"]] == ["bbb", "aaa", "ccc"]
    assert client.delete("/api/queue/" + queue[0]["entry"]).status_code == 404
    assert client.post("/api/queue/nope/move", json={"position": 0}).status_code == 404

    assert client.delete("/api/queue").json() == {"queue": []}


def test_only_ready_videos_can_be_queued(client, tmp_path):
    add_fake_video(client, tmp_path, "aaa", status="processing")
    assert client.post("/api/queue", json={"video_id": "aaa"}).status_code == 404
    assert client.post("/api/queue", json={"video_id": "nope"}).status_code == 404
    assert queue_names(client) == []


def test_queue_has_a_limit(client, tmp_path, monkeypatch):
    import main
    monkeypatch.setattr(main, "QUEUE_MAX", 2)
    add_fake_video(client, tmp_path, "aaa")
    for _ in range(2):
        assert client.post("/api/queue", json={"video_id": "aaa"}).status_code == 201
    assert client.post("/api/queue", json={"video_id": "aaa"}).status_code == 409


def test_deleted_videos_leave_the_queue(client, tmp_path):
    for video_id in ("aaa", "bbb", "ccc"):
        add_fake_video(client, tmp_path, video_id)
        client.post("/api/queue", json={"video_id": video_id})
    client.delete("/api/videos/aaa")
    assert queue_names(client) == ["bbb", "ccc"]
    client.post("/api/videos/delete", json={"ids": ["ccc"]})
    assert queue_names(client) == ["bbb"]
    stored = json.loads((tmp_path / "queue.json").read_text(encoding="utf-8"))
    assert [e["video_id"] for e in stored] == ["bbb"]


def test_queue_survives_restart(tmp_path):
    with make_client(tmp_path) as c:
        add_fake_video(c, tmp_path, "aaa")
        c.post("/api/queue", json={"video_id": "aaa"})
    with make_client(tmp_path) as c:
        assert queue_names(c) == ["aaa"]


def test_damaged_queue_file_is_ignored(tmp_path):
    (tmp_path / "queue.json").write_text('[{"oops": 1}]', encoding="utf-8")
    with make_client(tmp_path) as c:
        assert c.get("/api/queue").json() == {"queue": []}


def test_viewer_manages_the_queue_but_a_projector_does_not(client, viewer, projector, tmp_path):
    add_fake_video(client, tmp_path, "aaa")
    assert viewer.post("/api/queue", json={"video_id": "aaa"}).status_code == 201
    assert viewer.get("/api/queue").status_code == 200

    pairing = start_pairing(projector)
    client.post("/api/pair/approve", json=pair_key(pairing))
    pair_status(projector, pairing["token"])
    assert projector.get("/api/queue").status_code == 403
    assert projector.post("/api/queue", json={"video_id": "aaa"}).status_code == 403
    assert projector.delete("/api/queue").status_code == 403


def test_remotes_see_queue_changes(client, tmp_path):
    add_fake_video(client, tmp_path, "aaa")
    with client.websocket_connect("/ws?role=remote") as remote:
        assert remote.receive_json() == {"type": "tv", "connected": False}
        client.post("/api/queue", json={"video_id": "aaa"})
        update = remote.receive_json()
        assert update["type"] == "queue" and [q["video_id"] for q in update["queue"]] == ["aaa"]
    # A remote that connects later gets the queue right away.
    with client.websocket_connect("/ws?role=remote") as remote:
        remote.receive_json()
        assert remote.receive_json()["type"] == "queue"


def test_next_plays_the_queue_in_order(client, tmp_path):
    for video_id in ("aaa", "bbb"):
        add_fake_video(client, tmp_path, video_id)
        client.post("/api/queue", json={"video_id": video_id})
    with client.websocket_connect("/ws?role=tv") as tv, client.websocket_connect("/ws?role=remote") as remote:
        remote.receive_json()  # tv connected
        remote.receive_json()  # the queue
        remote.send_json({"type": "command", "action": "next"})
        assert tv.receive_json() == {"type": "command", "action": "load", "video_id": "aaa", "name": "aaa.mp4"}
        assert [q["video_id"] for q in remote.receive_json()["queue"]] == ["bbb"]

        # The projector reports the end of the video it plays: the next one starts by itself.
        tv.send_json(tv_state(video_id="aaa", name="aaa.mp4"))
        remote.receive_json()  # state
        tv.send_json({"type": "ended", "video_id": "aaa"})
        assert tv.receive_json()["video_id"] == "bbb"
        assert remote.receive_json() == {"type": "queue", "queue": []}

        # The queue is empty now: "next" says so.
        remote.send_json({"type": "command", "action": "next"})
        assert remote.receive_json() == {"type": "error", "message": "Черга порожня"}
    assert queue_names(client) == []


def test_a_stale_end_report_does_not_skip_a_video(client, tmp_path):
    for video_id in ("aaa", "bbb"):
        add_fake_video(client, tmp_path, video_id)
        client.post("/api/queue", json={"video_id": video_id})
    with client.websocket_connect("/ws?role=tv") as tv:
        tv.send_json(tv_state(video_id="zzz", name="zzz.mp4"))
        # Not what is playing, and nothing playing at all: both ignored.
        tv.send_json({"type": "ended", "video_id": "old"})
        tv.send_json(tv_state())
        tv.send_json({"type": "ended", "video_id": "zzz"})
        tv.send_json(tv_state(video_id="zzz", name="zzz.mp4"))
        tv.send_json({"type": "ended", "video_id": "zzz"})
        assert tv.receive_json()["video_id"] == "aaa"
    assert queue_names(client) == ["bbb"]


def test_next_skips_videos_deleted_from_the_queue_file(client, tmp_path):
    add_fake_video(client, tmp_path, "bbb")
    client.post("/api/queue", json={"video_id": "bbb"})
    # A queue entry whose video vanished behind the server's back.
    client.app.state.queue._entries.insert(0, {"entry": "x1", "video_id": "gone"})
    with client.websocket_connect("/ws?role=tv") as tv, client.websocket_connect("/ws?role=remote") as remote:
        remote.receive_json()
        remote.receive_json()
        remote.send_json({"type": "command", "action": "next"})
        assert tv.receive_json()["video_id"] == "bbb"
    assert queue_names(client) == []


def test_tv_page_reports_the_end_of_a_video(client):
    page = client.get("/tv").text
    assert "type: 'ended'" in page
    assert "addEventListener('ended', finished)" in page


def test_tv_page_retries_a_failed_video_before_giving_up(client):
    page = client.get("/tv").text
    # An error is not the end of the video: a network drop must not start the next one.
    assert "addEventListener('error', failed)" in page
    assert "addEventListener('error', finished)" not in page


# -------------------------------------------------------------------- folders


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
    import main
    monkeypatch.setattr(main, "FOLDERS_MAX", 2)
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


# -------------------------------------------------------------- video quality


def test_video_info_reads_the_finished_file():
    probe = fake_probe(avg_frame_rate="30000/1001")
    probe["streams"][1]["channels"] = 2
    assert video_info(probe) == {
        "width": 1920, "height": 1080, "fps": 29.97, "bitrate_kbps": 6000,
        "video_codec": "h264", "audio_codec": "aac", "channels": 2,
    }
    # No stream bitrate: the file's overall bitrate stands in. No audio: said so.
    silent = {"streams": [{"codec_type": "video", "codec_name": "h264", "width": 640, "height": 360,
                           "avg_frame_rate": "25/1"}], "format": {"bit_rate": "1500000"}}
    info = video_info(silent)
    assert info["bitrate_kbps"] == 1500 and info["audio_codec"] is None and info["channels"] == 0
    assert video_info({"streams": [{"codec_type": "audio"}]}) is None


@needs_ffmpeg
def test_processed_video_reports_its_quality(client, small_clip):
    video = wait_done(client, upload(client, small_clip).json()["id"])
    info = video["info"]
    assert (info["width"], info["height"]) == (320, 240)
    assert info["video_codec"] == "h264" and info["audio_codec"] == "aac"
    assert 29 < info["fps"] < 31 and info["bitrate_kbps"] > 0


@needs_ffmpeg
def test_quality_is_read_for_videos_processed_before(tmp_path, small_clip):
    for folder in ("uploads", "videos", "thumbs"):
        (tmp_path / folder).mkdir()
    (tmp_path / "videos" / "abc.mp4").write_bytes(small_clip)
    item = {
        "id": "abc", "name": "clip.mp4", "size": len(small_clip), "status": "ready",
        "created": 1.0, "source": "abc.mp4", "duration": 1.0, "thumb": False, "thumb_v": 2,
        "error": None,
    }
    (tmp_path / "videos.json").write_text(json.dumps({"abc": item}), encoding="utf-8")
    with make_client(tmp_path) as c:
        deadline = time.time() + 30
        while time.time() < deadline:
            video = c.get("/api/videos").json()["videos"][0]
            if video.get("info"):
                break
            time.sleep(0.1)
        assert video["info"]["width"] == 320


def test_quality_is_not_read_for_unfinished_or_missing_files(tmp_path):
    item = {
        "id": "abc", "name": "clip.mp4", "size": 1, "status": "ready", "created": 1.0,
        "source": "abc.mp4", "duration": 1.0, "thumb": False, "thumb_v": 2, "error": None,
    }
    (tmp_path / "videos.json").write_text(json.dumps({"abc": item}), encoding="utf-8")
    with make_client(tmp_path) as c:
        time.sleep(0.3)
        # The file is not there: the card simply has no quality, and nothing breaks.
        assert c.get("/api/videos").json()["videos"][0]["info"] is None


# ---------------------------------------------------------- durable data files


def test_write_json_keeps_the_previous_version(tmp_path):
    path = tmp_path / "data.json"
    write_json(path, {"v": 1})
    write_json(path, {"v": 2}, private=True)
    assert json.loads(path.read_text()) == {"v": 2}
    assert json.loads((tmp_path / "data.json.bak").read_text()) == {"v": 1}
    assert path.stat().st_mode & 0o777 == 0o600
    assert not (tmp_path / "data.json.tmp").exists()


def test_damaged_file_falls_back_to_the_backup_and_is_kept(tmp_path):
    path = tmp_path / "data.json"
    write_json(path, {"v": 1})
    write_json(path, {"v": 2})
    path.write_text('{"v": 2', encoding="utf-8")  # cut short by a power cut
    assert read_json(path, dict) == {"v": 1}
    # The damaged file is moved aside, not lost and not overwritten later.
    broken = list(tmp_path.glob("data.json.broken-*"))
    assert len(broken) == 1 and broken[0].read_text() == '{"v": 2'
    assert not path.exists()


def test_finished_tmp_file_wins_over_the_backup(tmp_path):
    # A power cut between moving the old file to .bak and the new one into place.
    path = tmp_path / "data.json"
    (tmp_path / "data.json.bak").write_text('{"v": 1}')
    (tmp_path / "data.json.tmp").write_text('{"v": 2}')
    assert read_json(path, dict) == {"v": 2}
    # A half-written .tmp is skipped.
    (tmp_path / "data.json.tmp").write_text('{"v": ')
    assert read_json(path, dict) == {"v": 1}


def test_nothing_usable_reads_as_none(tmp_path):
    path = tmp_path / "data.json"
    assert read_json(path, dict) is None
    path.write_text("[]")
    assert read_json(path, lambda d: d["x"]) is None


def test_video_list_survives_a_damaged_file(tmp_path):
    with make_client(tmp_path) as c:
        add_fake_video(c, tmp_path, "aaa")
        add_fake_video(c, tmp_path, "bbb")
    (tmp_path / "videos.json").write_text('{"aaa": {"id": "aaa", "na', encoding="utf-8")
    with make_client(tmp_path) as c:
        # The previous version: written before "bbb" was added.
        assert video_ids(c) == {"aaa"}


def test_damaged_users_file_does_not_stop_the_service(tmp_path):
    (tmp_path / "users.json").write_text("garbage", encoding="utf-8")
    with make_client(tmp_path) as c:
        assert c.get("/api/me").json()["username"] == "admin"


# --------------------------------------------------------- stuck ffmpeg/ffprobe


def fake_tool(tmp_path: Path, name: str) -> str:
    """A stand-in for ffmpeg or ffprobe that hangs without a word."""
    path = tmp_path / name
    path.write_text("#!/bin/sh\nexec sleep 30\n")
    path.chmod(0o755)
    return str(path)


@needs_ffmpeg
def test_stuck_ffmpeg_is_stopped(client, tmp_path, small_clip):
    transcoder = client.app.state.transcoder
    transcoder.ffmpeg = fake_tool(tmp_path, "ffmpeg")
    transcoder.stall_seconds = 1
    started = time.time()
    video = wait_done(client, upload(client, small_clip).json()["id"], timeout=20)
    assert video["status"] == "error" and "завис" in video["error"]
    assert time.time() - started < 15
    assert not list((tmp_path / "videos").glob("*.tmp.mp4"))


def test_stuck_ffprobe_is_stopped(client, tmp_path, monkeypatch):
    import main
    monkeypatch.setattr(main, "PROBE_TIMEOUT_SECONDS", 1)
    transcoder = client.app.state.transcoder
    transcoder.ffmpeg = transcoder.ffmpeg or "/bin/false"
    transcoder.ffprobe = fake_tool(tmp_path, "ffprobe")
    video = wait_done(client, upload(client, b"x" * 100).json()["id"], timeout=20)
    assert video["status"] == "error" and "ffprobe" in video["error"]


# ------------------------------------------------- losing access closes the remote


def remote_closes(ws) -> int:
    with pytest.raises(WebSocketDisconnect) as closed:
        while True:
            ws.receive_json()
    return closed.value.code


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



# ------------------------------------------------------ leftovers of the review


def test_batch_delete_writes_the_list_once(client, tmp_path, monkeypatch):
    import main

    for i in range(20):
        add_fake_video(client, tmp_path, "v%02d" % i)
    writes = []
    real = main.write_json
    monkeypatch.setattr(main, "write_json", lambda path, *a, **kw: (writes.append(path.name), real(path, *a, **kw)))
    r = client.post("/api/videos/delete", json={"ids": ["v%02d" % i for i in range(20)]})
    assert len(r.json()["deleted"]) == 20
    assert writes.count("videos.json") == 1
    assert video_ids(client) == set()


def test_deleting_a_folder_writes_the_list_once(client, tmp_path, monkeypatch):
    import main

    folder = make_folder(client, "Мультики")
    for i in range(10):
        add_fake_video(client, tmp_path, "v%02d" % i)
        client.post("/api/videos/v%02d/folder" % i, json={"folder": folder})
    writes = []
    real = main.write_json
    monkeypatch.setattr(main, "write_json", lambda path, *a, **kw: (writes.append(path.name), real(path, *a, **kw)))
    assert client.delete("/api/folders/" + folder).status_code == 204
    assert writes.count("videos.json") == 1
    assert all(v["folder"] is None for v in library(client)["videos"])


@pytest.mark.parametrize(
    "name, content",
    [
        ("videos.json", {"aaa": {"id": "aaa", "name": "a.mp4"}}),
        ("videos.json", {"aaa": {"id": "aaa", "name": "a.mp4", "status": "ready", "created": "today"}}),
        ("users.json", {"olena": {"role": "viewer"}}),
        ("sessions.json", {"abc": {"username": "admin"}}),
        ("devices.json", {"abc": {"name": "Проектор"}}),
    ],
)
def test_file_of_the_wrong_shape_counts_as_damaged(tmp_path, name, content):
    (tmp_path / name).write_text(json.dumps(content), encoding="utf-8")
    with make_client(tmp_path) as c:
        assert c.get("/api/videos").json()["videos"] == []
        assert c.get("/api/users").status_code == 200
        assert c.get("/api/devices").json()["devices"] == []
    # Kept aside for a person to look at, not overwritten.
    assert list(tmp_path.glob(name + ".broken-*"))


def test_video_without_a_source_fails_instead_of_stopping_the_worker(client, tmp_path):
    client.app.state.store.add({
        "id": "old", "name": "old.mp4", "size": 1, "status": "error", "created": time.time(),
    })
    assert client.post("/api/videos/old/retry").status_code == 409
    client.app.state.store.update("old", status="queued")
    client.app.state.transcoder.enqueue("old")
    assert wait_done(client, "old")["status"] == "error"


def test_unexpected_failure_leaves_no_unfinished_file(client, tmp_path, small_clip, monkeypatch):
    import main

    def explode(ffmpeg, src, dst, *args):
        dst.write_bytes(b"half")
        raise RuntimeError("boom")

    monkeypatch.setattr(main, "build_ffmpeg_args", explode)
    video = upload(client, small_clip).json()
    assert wait_done(client, video["id"])["status"] == "error"
    assert not list((tmp_path / "videos").glob("*.tmp.mp4"))


def test_projector_cannot_act_as_a_remote(client, projector):
    pairing = start_pairing(projector)
    client.post("/api/pair/approve", json=pair_key(pairing))
    pair_status(projector, pairing["token"])
    with projector.websocket_connect("/ws?role=tv"):
        pass
    with projector.websocket_connect("/ws?role=remote") as ws:
        assert remote_closes(ws) == 1008


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
    import main

    monkeypatch.setattr(main, "LIMITER_MAX_ADDRESSES", 10)
    limiter = main.LoginLimiter(max_failures=5, window=0.05)
    for i in range(10):
        limiter.fail("10.0.0.%d" % i)
    time.sleep(0.1)
    limiter.fail("10.0.1.1")
    assert list(limiter._failures) == ["10.0.1.1"]


# ---------------------------------------------------------------- projector check


def played(clip_id: str, advance: float = 1.0, frames: int = 180, dropped: int = 0) -> dict:
    return {
        "id": clip_id, "outcome": "played", "advance": advance, "frames": frames,
        "dropped": dropped,
    }


def test_check_levels_grow_and_depend_on_easier_ones():
    levels = tvtest_levels()
    assert [level["id"] for level in levels][:4] == ["720p30", "720p60", "1080p30", "1080p60"]
    needs = {level["id"]: level["needs"] for level in levels}
    assert needs["720p30"] is None
    assert needs["1080p60"] == "1080p30" and needs["2160p30"] == "1440p30"
    assert {level["height"]: level["kbps"] for level in levels}[1080] == 8000


def test_check_report_gets_a_verdict_for_every_clip():
    report = {
        "speed_kbps": 23456.7,
        "device": {"agent": "Tizen " * 100, "screen_width": 1920, "screen_height": 1080},
        "clips": [
            played("720p30"),
            played("720p60", frames=360, dropped=18),      # exactly the allowed 5%
            played("1080p30", advance=0.97, frames=0),     # no frame counters: the clock decides
            played("1080p60", frames=360, dropped=40),
            played("1440p30", advance=0.7),
            {"id": "1440p60", "outcome": "skipped"},
            {"id": "2160p30", "outcome": "failed", "reason": "download"},
            {"id": "nonsense", "outcome": "played", "advance": 1},
            "not a clip",
        ],
    }
    result = clean_tvtest_report(report, tvtest_levels())
    status = {clip["id"]: clip["status"] for clip in result["clips"]}
    assert status == {
        "720p30": "ok", "720p60": "ok", "1080p30": "ok", "1080p60": "rough",
        "1440p30": "rough", "1440p60": "skipped", "2160p30": "failed",
        "2160p60": "skipped",  # not reported at all
    }
    assert result["clips"][6]["reason"] == "download"
    assert result["speed_kbps"] == 23456
    assert len(result["device"]["agent"]) == 300
    assert tvtest_fits(result) == {"max_height": 1080, "max_fps": 30, "network_kbps": 11728}


def test_check_report_survives_junk():
    junk = {"clips": [{"id": "720p30", "outcome": "played", "advance": "NaN", "frames": []}],
            "device": "tv", "speed_kbps": float("inf")}
    result = clean_tvtest_report(junk, tvtest_levels())
    assert result["clips"][0]["status"] == "rough"
    assert result["speed_kbps"] is None and result["device"]["agent"] == ""
    assert tvtest_fits(result) == {"max_height": None, "max_fps": None, "network_kbps": None}
    assert clean_tvtest_report({}, tvtest_levels())["clips"][0]["status"] == "skipped"


def test_check_needs_an_idle_started_projector(client):
    assert client.get("/api/tvtest").json() == {
        "status": "idle", "step": 0, "steps": 8, "error": None, "result": None,
    }
    assert client.post("/api/tvtest").status_code == 409  # no projector
    with client.websocket_connect("/ws?role=tv") as tv:
        tv.send_json(tv_state(started=False))
        client.get("/api/me")  # lets the server read the state
        assert "Почати" in client.post("/api/tvtest").json()["detail"]
        tv.send_json(tv_state(video_id="abc", name="Clip"))
        client.get("/api/me")
        assert "зупиніть" in client.post("/api/tvtest").json()["detail"]


def wait_check(client: TestClient, status: str, timeout: float = 60) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        view = client.get("/api/tvtest").json()
        if view["status"] == status:
            return view
        time.sleep(0.05)
    raise AssertionError("the check did not become %s" % status)


@needs_ffmpeg
def test_check_runs_on_the_projector_and_is_remembered(client, tmp_path):
    client.app.state.tvtest.levels = tvtest_levels()[:2]
    with client.websocket_connect("/ws?role=tv") as tv:
        tv.send_json(tv_state())
        client.get("/api/me")
        r = client.post("/api/tvtest")
        assert r.status_code == 202 and r.json()["status"] == "preparing"
        assert client.post("/api/tvtest").status_code == 409  # one at a time

        command = tv.receive_json()
        assert command["action"] == "test" and command["seconds"] > 0
        assert [clip["id"] for clip in command["clips"]] == ["720p30", "720p60"]
        assert wait_check(client, "running")["step"] == 0

        # The clip is what the settings promise: H.264 at that size, about 4 Mbit/s.
        clip = client.get(command["clips"][0]["url"])
        assert clip.status_code == 200 and clip.headers["content-type"] == "video/mp4"
        (tmp_path / "clip.mp4").write_bytes(clip.content)
        video = probe_video(tmp_path / "clip.mp4")
        assert (video["codec_name"], video["width"], video["height"]) == ("h264", 1280, 720)
        assert 3_000_000 < len(clip.content) < 5_500_000
        speed = client.get(command["speed"]["url"])
        assert len(speed.content) == 4 * 1024 * 1024
        assert client.get("/tvtest/clip/..%2Fvideos.json").status_code == 404

        tv.send_json({"type": "test", "run": "someone-else", "done": True, "clips": []})
        tv.send_json({"type": "test", "run": command["run"], "step": 2})
        assert wait_check(client, "running")["result"] is None  # the foreign report is ignored
        tv.send_json({
            "type": "test", "run": command["run"], "done": True, "speed_kbps": 40000,
            "device": {"agent": "TestTV", "screen_width": 1920, "screen_height": 1080},
            "clips": [played("720p30"), played("720p60", advance=0.5)],
        })
        view = wait_check(client, "idle")
    result = view["result"]
    assert view["error"] is None
    assert [clip["status"] for clip in result["clips"]] == ["ok", "rough"]
    assert result["fits"] == {"max_height": 720, "max_fps": 30, "network_kbps": 20000}
    assert result["device"]["agent"] == "TestTV"

    # The result is still there after a restart.
    with make_client(tmp_path) as again:
        assert again.get("/api/tvtest").json()["result"] == result


@needs_ffmpeg
def test_check_fails_when_the_projector_leaves_or_gets_busy(client):
    client.app.state.tvtest.levels = tvtest_levels()[:1]
    with client.websocket_connect("/ws?role=tv") as tv:
        tv.send_json(tv_state())
        client.get("/api/me")
        assert client.post("/api/tvtest").status_code == 202
        command = tv.receive_json()
        tv.send_json({"type": "test", "run": command["run"], "aborted": True})
        view = wait_check(client, "idle")
        assert "перервано" in view["error"] and view["result"] is None

        assert client.post("/api/tvtest").status_code == 202
        tv.receive_json()
        wait_check(client, "running")
    assert "від'єднався" in wait_check(client, "idle")["error"]


def test_check_gives_up_on_a_silent_projector(client):
    check = client.app.state.tvtest
    check.status, check.run, check.seen = "running", "abc", time.time() - 3600
    view = client.get("/api/tvtest").json()
    assert view["status"] == "idle" and "перестав відповідати" in view["error"]


def test_check_is_started_by_people_and_run_by_projectors(client, anon, projector):
    assert anon.get("/api/tvtest").status_code == 401
    assert anon.get("/tvtest/speed", follow_redirects=False).status_code == 303
    pairing = start_pairing(projector)
    client.post("/api/pair/approve", json=pair_key(pairing))
    pair_status(projector, pairing["token"])
    assert projector.get("/api/tvtest").status_code == 403
    assert projector.post("/api/tvtest").status_code == 403
    assert projector.get("/tvtest/speed").status_code == 200
