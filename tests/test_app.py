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
    ProcessingError,
    Settings,
    build_ffmpeg_args,
    create_app,
    plan_encode,
    thumbnail_args,
)

TOKEN = "test-token-1234567890"
FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="потрібен ffmpeg")


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
        # Сесії лежать у теці з даними, тож cookie діє і в застосунку, запущеному після.
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
    raise AssertionError("обробка не завершилася за %s с" % timeout)


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


# ------------------------------------------------------------ рішення за ffprobe


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


SETTINGS = Settings(data_dir=Path("."), token=TOKEN)


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
    # AAC 5.1 теж зводимо до стерео, не чіпаючи картинку.
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
    assert args[args.index("-maxrate") + 1] == "8000k"
    assert "-force_key_frames" in args
    assert args[args.index("-color_trc") + 1] == "bt709"


def test_remux_args_copy_streams():
    plan = plan_encode(fake_probe(), SETTINGS)
    args = build_ffmpeg_args("ffmpeg", Path("in.mp4"), Path("out.mp4"), plan, SETTINGS, None)
    assert args[args.index("-c") + 1] == "copy"
    assert "-vf" not in args
    assert "+faststart" in args


# ---------------------------------------------------------------- сервіс цілком


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

    # Оригінал прибрано, готовий файл названий сервером.
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
    # Оригінал лишається, щоб можна було повторити.
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
    time.sleep(1.5)  # воркер має помітити видалення і прибрати за собою
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


# -------------------------------------------------------------------------- вхід


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
    # Сторінка проектора відкрита: без входу вона лише показує код підключення.
    r = anon.get("/tv")
    assert r.status_code == 200
    assert "pair-code" in r.text


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
    assert "max-age=31536000" in cookie
    assert TOKEN not in r.headers["set-cookie"]
    assert anon.get("/api/videos").status_code == 200
    assert anon.get("/login").status_code == 303  # уже увійшли


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
    # Після п'яти невдач не пускає навіть правильний пароль.
    assert sign_in(anon).status_code == 429
    assert anon.get("/api/videos").status_code == 401


def test_successful_login_does_not_reset_earlier_failures(client):
    assert add_user(client).status_code == 201
    anon = TestClient(client.app, follow_redirects=False)
    # Власний вдалий вхід не дає глядачу нових спроб підбирати чужий пароль.
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
        # У цю мить пароль ще перевіряється, а спробу вже записано.
        limiter_hits.append(sign_in_blocked_after(anon, 4))
        return original(username)

    anon.app.state.users.get = spy
    assert sign_in(anon, "nobody", "whatever-123").status_code == 401
    assert limiter_hits == [True]


def sign_in_blocked_after(client: TestClient, more: int) -> bool:
    """Чи блокує вхід після ще `more` невдалих спроб."""
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
    # У файлі лежить лише хеш секрету.
    stored = (tmp_path / "sessions.json").read_text(encoding="utf-8")
    assert first.split(".", 1)[1] not in stored and TOKEN not in stored


def test_logout_revokes_the_session_on_the_server(anon):
    cookie = sign_in(anon).cookies["promin_session"]
    assert anon.post("/logout").status_code == 303
    # Навіть якщо cookie хтось зберіг, вона більше не діє.
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
    # Сесією користувалися — позначка оновлюється, і вона живе далі.
    sessions._sessions[session_id]["seen"] -= 29 * 24 * 3600
    assert anon.get("/api/me").status_code == 200
    assert time.time() - sessions.get(session_id)["seen"] < 60
    # Не користувалися понад 30 днів — сесії немає.
    sessions._sessions[session_id]["seen"] -= 31 * 24 * 3600
    assert anon.get("/api/me").status_code == 401
    assert sessions.get(session_id) is None


def test_changing_token_ends_admin_sessions(tmp_path):
    with make_client(tmp_path) as c:
        assert c.get("/api/me").status_code == 200
        cookie = c.cookies["promin_session"]
    other = Settings(data_dir=tmp_path, token="another-token-1234567890")
    with TestClient(create_app(other)) as c:
        c.cookies.set("promin_session", cookie)
        assert c.get("/api/me").status_code == 401


# ---------------------------------------------------------- користувачі й ролі

VIEWER_PASSWORD = "viewer-pass-1"


def add_user(client: TestClient, username: str = "olena", password: str = VIEWER_PASSWORD, role: str = "viewer"):
    return client.post("/api/users", json={"username": username, "password": password, "role": role})


@pytest.fixture
def viewer(client, tmp_path):
    """Клієнт, що увійшов як глядач; `client` поруч лишається адміністратором."""
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
    # Пароль у файлі не лежить відкритим текстом.
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
def test_viewer_can_watch_but_not_change(client, viewer, small_clip):
    video = wait_done(client, upload(client, small_clip).json()["id"])
    assert video["status"] == "ready"

    assert [v["id"] for v in viewer.get("/api/videos").json()["videos"]] == [video["id"]]
    assert viewer.get("/").status_code == 200
    assert viewer.get("/media/" + video["id"]).status_code == 200
    assert viewer.get("/thumb/" + video["id"]).status_code == 200

    assert upload(viewer, small_clip).status_code == 403
    assert viewer.delete("/api/videos/" + video["id"]).status_code == 403
    assert viewer.post("/api/videos/%s/retry" % video["id"]).status_code == 403
    assert len(client.get("/api/videos").json()["videos"]) == 1


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


# ------------------------------------------------------------------------ пульт

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
    # Сторінка з іншого сайту чи іншого порту цього ж сервера пульт не відкриє.
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
    # Глядач теж може керувати показом.
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

        # Невідоме відео, невідома дія і сміття до проектора не доходять.
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
                # Команди йдуть уже новому проектору.
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


# ------------------------------------------------------ підключення проектора кодом


def start_pairing(tv: TestClient) -> dict:
    r = tv.post("/api/pair/start")
    assert r.status_code == 200
    return r.json()


def pair_status(tv: TestClient, token: str) -> str:
    return tv.post("/api/pair/status", json={"token": token}).json()["status"]


@pytest.fixture
def projector(client):
    """Клієнт без входу, який грає роль браузера проектора."""
    return TestClient(client.app, follow_redirects=False)


def test_projector_is_paired_by_code(client, projector):
    assert projector.get("/api/me").status_code == 401
    pairing = start_pairing(projector)
    assert len(pairing["code"]) == 6 and pairing["code"].isdigit()
    assert pair_status(projector, pairing["token"]) == "pending"

    # Код вводять на телефоні, де вже увійшли; пробіл посередині не заважає.
    spaced = pairing["code"][:3] + " " + pairing["code"][3:]
    assert client.post("/api/pair/approve", json={"code": spaced}).status_code == 200

    r = projector.post("/api/pair/status", json={"token": pairing["token"]})
    assert r.json()["status"] == "approved"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "max-age=31536000" in cookie

    me = projector.get("/api/me").json()
    assert me["role"] == "device"
    with projector.websocket_connect("/ws?role=tv") as tv, client.websocket_connect("/ws?role=remote") as remote:
        assert remote.receive_json() == {"type": "tv", "connected": True}
        remote.send_json({"type": "command", "action": "pause"})
        assert tv.receive_json() == {"type": "command", "action": "pause"}


def test_code_works_only_once(client, projector):
    pairing = start_pairing(projector)
    assert client.post("/api/pair/approve", json={"code": pairing["code"]}).status_code == 200
    assert client.post("/api/pair/approve", json={"code": pairing["code"]}).status_code == 404
    assert pair_status(projector, pairing["token"]) == "approved"
    assert pair_status(projector, pairing["token"]) == "expired"


def test_wrong_or_unknown_code_is_rejected(client, projector):
    pairing = start_pairing(projector)
    wrong = "%06d" % ((int(pairing["code"]) + 1) % 10**6)
    assert client.post("/api/pair/approve", json={"code": wrong}).status_code == 404
    assert client.post("/api/pair/approve", json={"code": "12"}).status_code == 404
    assert pair_status(projector, pairing["token"]) == "pending"
    assert pair_status(projector, "no-such-token") == "expired"


def test_code_expires(client, projector):
    pairing = start_pairing(projector)
    client.app.state.pairing.pending[pairing["token"]]["created"] -= 11 * 60
    assert client.post("/api/pair/approve", json={"code": pairing["code"]}).status_code == 404
    assert pair_status(projector, pairing["token"]) == "expired"


def test_approving_requires_a_logged_in_user(client, projector, viewer):
    pairing = start_pairing(projector)
    assert projector.post("/api/pair/approve", json={"code": pairing["code"]}).status_code == 401
    # Глядач теж може підключити проектор.
    assert viewer.post("/api/pair/approve", json={"code": pairing["code"]}).status_code == 200
    assert pair_status(projector, pairing["token"]) == "approved"
    # А сам проектор підключати інші проектори не може.
    other = TestClient(client.app, follow_redirects=False)
    second = start_pairing(other)
    assert projector.post("/api/pair/approve", json={"code": second["code"]}).status_code == 403


def test_projector_can_only_show(client, projector):
    pairing = start_pairing(projector)
    client.post("/api/pair/approve", json={"code": pairing["code"]})
    pair_status(projector, pairing["token"])

    assert projector.get("/api/videos").status_code == 200
    assert upload(projector, b"x" * 100).status_code == 403
    assert projector.get("/api/users").status_code == 403
    assert projector.get("/api/devices").status_code == 403
    assert add_user(projector, "petro").status_code == 403


def test_admin_can_disconnect_projector(client, projector):
    pairing = start_pairing(projector)
    client.post("/api/pair/approve", json={"code": pairing["code"]})
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
    client.post("/api/pair/approve", json={"code": pairing["code"]})
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
        c.post("/api/pair/approve", json={"code": pairing["code"]})
        cookie = tv.post("/api/pair/status", json={"token": pairing["token"]}).cookies["promin_session"]
        # Секрет пристрою у файлі не лежить відкритим текстом.
        assert cookie.split(".", 1)[1] not in (tmp_path / "devices.json").read_text(encoding="utf-8")
    with make_client(tmp_path, login=False) as c:
        c.cookies.set("promin_session", cookie)
        assert c.get("/api/me").json()["role"] == "device"


# -------------------------------------------------------------------- мініатюри


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


# ------------------------------------------------------- перекодування лише звуку


def stream_md5(path: Path, stream: str) -> str:
    """Контрольна сума доріжки без перекодування: однакова, якщо доріжку скопійовано."""
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
    # Відеодоріжка та сама, біт у біт.
    assert stream_md5(ready, "0:v:0") == stream_md5(source, "0:v:0")
