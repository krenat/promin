from __future__ import annotations

import time

from fastapi.testclient import TestClient

from helpers import (
    make_client,
    needs_ffmpeg,
    pair_key,
    pair_status,
    probe_video,
    start_pairing,
    tv_state,
)
from promin.tvtest import clean_tvtest_report, tvtest_fits, tvtest_levels


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
    client.app.state.services.tvtest.levels = tvtest_levels()[:2]
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
    client.app.state.services.tvtest.levels = tvtest_levels()[:1]
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
    check = client.app.state.services.tvtest
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
