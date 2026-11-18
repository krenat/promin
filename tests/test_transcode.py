"""Processing a video: the plan from ffprobe, the ffmpeg command, thumbnails, quality."""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from helpers import FFMPEG, make_client, needs_ffmpeg, upload, wait_done
from promin.encoding import Encoding
from promin.ffmpeg import (
    HDR_FILTER_ZSCALE,
    ProcessingError,
    build_ffmpeg_args,
    plan_encode,
    thumbnail_args,
    video_info,
)


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


def fake_tool(tmp_path: Path, name: str) -> str:
    """A stand-in for ffmpeg or ffprobe that hangs without a word."""
    path = tmp_path / name
    path.write_text("#!/bin/sh\nexec sleep 30\n")
    path.chmod(0o755)
    return str(path)


@needs_ffmpeg
def test_stuck_ffmpeg_is_stopped(client, tmp_path, small_clip):
    transcoder = client.app.state.services.transcoder
    transcoder.ffmpeg = fake_tool(tmp_path, "ffmpeg")
    transcoder.stall_seconds = 1
    started = time.time()
    video = wait_done(client, upload(client, small_clip).json()["id"], timeout=20)
    assert video["status"] == "error" and "завис" in video["error"]
    assert time.time() - started < 15
    assert not list((tmp_path / "videos").glob("*.tmp.mp4"))


def test_stuck_ffprobe_is_stopped(client, tmp_path, monkeypatch):
    from promin import transcode
    monkeypatch.setattr(transcode, "PROBE_TIMEOUT_SECONDS", 1)
    transcoder = client.app.state.services.transcoder
    transcoder.ffmpeg = transcoder.ffmpeg or "/bin/false"
    transcoder.ffprobe = fake_tool(tmp_path, "ffprobe")
    video = wait_done(client, upload(client, b"x" * 100).json()["id"], timeout=20)
    assert video["status"] == "error" and "ffprobe" in video["error"]


def test_video_without_a_source_fails_instead_of_stopping_the_worker(client, tmp_path):
    client.app.state.services.videos.add({
        "id": "old", "name": "old.mp4", "size": 1, "status": "error", "created": time.time(),
    })
    assert client.post("/api/videos/old/retry").status_code == 409
    client.app.state.services.videos.update("old", status="queued")
    client.app.state.services.transcoder.enqueue("old")
    assert wait_done(client, "old")["status"] == "error"


def test_unexpected_failure_leaves_no_unfinished_file(client, tmp_path, small_clip, monkeypatch):
    from promin import transcode

    def explode(ffmpeg, src, dst, *args):
        dst.write_bytes(b"half")
        raise RuntimeError("boom")

    monkeypatch.setattr(transcode, "build_ffmpeg_args", explode)
    video = upload(client, small_clip).json()
    assert wait_done(client, video["id"])["status"] == "error"
    assert not list((tmp_path / "videos").glob("*.tmp.mp4"))
