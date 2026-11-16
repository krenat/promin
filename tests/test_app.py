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
        # Cookie, яку сервер видає після входу, однакова для того самого токена.
        with TestClient(create_app(settings), follow_redirects=False) as tmp:
            r = tmp.post("/login", data={"token": TOKEN})
            assert r.status_code == 303
            client.cookies.set("promin_session", r.cookies["promin_session"])
    return client


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


def test_non_aac_audio_is_transcoded():
    probe = fake_probe()
    probe["streams"][1]["codec_name"] = "opus"
    assert plan_encode(probe, SETTINGS)["mode"] == "transcode"


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


def test_api_requires_login(anon, tmp_path):
    assert anon.get("/api/videos").status_code == 401
    assert anon.delete("/api/videos/abc").status_code == 401
    assert anon.post("/api/videos/abc/retry").status_code == 401
    assert upload(anon, b"x" * 100).status_code == 401
    assert list((tmp_path / "uploads").iterdir()) == []


def test_login_page_and_logo_are_public(anon):
    r = anon.get("/login")
    assert r.status_code == 200
    assert 'name="token"' in r.text
    assert anon.get("/static/logo.svg").status_code == 200


def test_login_sets_long_lived_http_only_cookie(anon):
    r = anon.post("/login", data={"token": TOKEN})
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
    r = anon.post("/login", data={"token": "wrong-token-000000000"})
    assert r.status_code == 401
    assert "Неправильний токен" in r.text
    assert "set-cookie" not in r.headers
    assert anon.get("/api/videos").status_code == 401


def test_forged_cookie_is_rejected(anon):
    anon.cookies.set("promin_session", "0" * 64)
    assert anon.get("/api/videos").status_code == 401


def test_login_attempts_are_limited(anon):
    for _ in range(5):
        assert anon.post("/login", data={"token": "wrong"}).status_code == 401
    # Після п'яти невдач не пускає навіть правильний токен.
    assert anon.post("/login", data={"token": TOKEN}).status_code == 429
    assert anon.get("/api/videos").status_code == 401
