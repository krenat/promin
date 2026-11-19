"""What the test files share: clients, clips and small steps through the API."""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from promin.app import create_app
from promin.config import Settings

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


def another(client: TestClient) -> TestClient:
    """One more browser of the same running app: its own cookies, and the same event loop.
    A client made on its own runs every request and WebSocket in a loop of its own, and a
    message sent from one loop to a socket waiting in another may never wake it."""
    other = TestClient(client.app, follow_redirects=False)
    other.portal = client.portal
    return other


def sign_in(client: TestClient, username: str = "admin", password: str = TOKEN):
    return client.post("/login", data={"username": username, "password": password})


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


VIEWER_PASSWORD = "viewer-pass-1"


def add_user(client: TestClient, username: str = "olena", password: str = VIEWER_PASSWORD, role: str = "viewer"):
    return client.post("/api/users", json={"username": username, "password": password, "role": role})


def tv_state(**fields) -> dict:
    state = {
        "type": "state", "video_id": None, "name": None, "position": 0, "duration": 0,
        "paused": True, "volume": 1, "started": True,
    }
    state.update(fields)
    return state


def start_pairing(tv: TestClient) -> dict:
    r = tv.post("/api/pair/start")
    assert r.status_code == 200
    return r.json()


def pair_key(pairing: dict) -> dict:
    """What the phone sends after scanning: the key from the address in the QR code."""
    return {"key": pairing["url"].split("?t=")[1]}


def pair_status(tv: TestClient, token: str) -> str:
    return tv.post("/api/pair/status", json={"token": token}).json()["status"]


NEW_ENCODING = {
    "max_height": 2160, "max_fps": 30, "max_bitrate_kbps": 30000, "crf": 19,
    "audio_bitrate_kbps": 192, "x264_preset": "fast",
}


def add_fake_video(client: TestClient, tmp_path: Path, video_id: str, status: str = "ready") -> None:
    """Puts a video straight into the store, with files, without running ffmpeg."""
    store = client.app.state.services.videos
    store.add({
        "id": video_id, "name": video_id + ".mp4", "size": 1, "status": status,
        "created": time.time(), "source": video_id + ".mp4", "duration": 1.0,
        "thumb": True, "error": None,
    })
    (tmp_path / "videos" / (video_id + ".mp4")).write_bytes(b"v")
    (tmp_path / "thumbs" / (video_id + ".jpg")).write_bytes(b"t")


def stored(client: TestClient, sql: str, *params):
    """Rows straight from the data file, past the stores."""
    return client.app.state.services.db.all(sql, params)


def change_stored(client: TestClient, sql: str, *params) -> None:
    """Changes the data file behind the server's back."""
    client.app.state.services.db.run(sql, params)


def count_commits(client: TestClient) -> list:
    """Counts the transactions stored from now on: the list it returns grows by one each."""
    commits: list = []
    client.app.state.services.db.conn.set_trace_callback(
        lambda statement: commits.append(statement) if statement == "COMMIT" else None
    )
    return commits


def video_ids(client: TestClient) -> set:
    return {v["id"] for v in client.get("/api/videos").json()["videos"]}


def remote_closes(ws) -> int:
    with pytest.raises(WebSocketDisconnect) as closed:
        while True:
            ws.receive_json()
    return closed.value.code
