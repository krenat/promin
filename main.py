"""Promin, a home video server for a projector.

Video list, upload, transcoding, playback, deletion, users with roles and a remote:
the phone controls the projector page over WebSocket. The projector is paired with
a QR code from its screen that is scanned and approved on the phone.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import io
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, unquote, urlsplit

import anyio
import segno
from fastapi import (
    APIRouter, Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect,
)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.requests import ClientDisconnect, HTTPConnection

log = logging.getLogger("promin")

BASE_DIR = Path(__file__).resolve().parent
MB = 1024 * 1024

ALLOWED_EXT = {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".3gp"}
PUBLIC_KEYS = (
    "id", "name", "size", "status", "created", "duration", "thumb", "thumb_v", "error", "folder",
    "info",
)

# Version of the thumbnail method: after it changes, old thumbnails are remade.
THUMB_VERSION = 2

COOKIE_NAME = "promin_session"
# A user session ends after this long without use, and so does a paired projector.
SESSION_IDLE_TTL = 30 * 24 * 3600
DEVICE_IDLE_TTL = SESSION_IDLE_TTL
# The browser keeps the cookie just as long; every page load renews it (see /api/me).
COOKIE_MAX_AGE = SESSION_IDLE_TTL
SESSION_TOUCH_EVERY = 24 * 3600  # how often to record that a session was used
LOGIN_MAX_BODY = 4096
BATCH_DELETE_MAX = 500
QUEUE_MAX = 200
FOLDERS_MAX = 100
# How long ffmpeg and ffprobe may go without a sign of life before they are stopped.
FFMPEG_STALL_SECONDS = 10 * 60
PROBE_TIMEOUT_SECONDS = 2 * 60
THUMB_TIMEOUT_SECONDS = 2 * 60
FOLDER_NAME_MAX = 60
MIN_TOKEN_LENGTH = 12

# Where login may redirect to, and which commands the remote may send to the projector.
LOGIN_NEXT = ("/", "/tv")
REMOTE_ACTIONS = ("load", "play", "pause", "seek", "seek_by", "volume", "stop", "next")
WS_REPLACED = 4000  # the projector page was opened elsewhere
WS_UNAUTHORIZED = 4401

# Pairing a projector with a QR code: how long one lives and how many may wait at once.
PAIR_CODE_TTL = 10 * 60
PAIR_MAX_PENDING = 500
PAIR_STARTS_PER_WINDOW = 10
# The address inside the QR code: login may return to it, with a well-formed key only.
PAIR_NEXT_RE = re.compile(r"/pair\?t=[A-Za-z0-9_-]{16,64}")

# Built-in administrator: its password is PROMIN_TOKEN and is never stored in a file.
ADMIN = "admin"
ROLES = ("admin", "viewer")
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")
MIN_PASSWORD_LENGTH = 8
PBKDF2_ROUNDS = 200_000
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SECONDS = 15 * 60
LIMITER_MAX_ADDRESSES = 5000  # past this many, addresses with no recent failures are forgotten

# What the administrator may choose on the settings page. The frame is 16:9.
HEIGHTS = {720: 1280, 1080: 1920, 1440: 2560, 2160: 3840}  # height → width
FPS_CHOICES = (30, 60)
CRF_RANGE = (16, 28)
BITRATE_RANGE_KBPS = (1000, 60000)
AUDIO_BITRATES_KBPS = (96, 128, 160, 192, 256)
X264_PRESETS = ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow")
# Codecs that need fewer bits than H.264 for the same picture: when transcoding from them,
# the bitrate ceiling is the source bitrate with this headroom.
EFFICIENT_CODECS = {"hevc", "av1", "vp9", "vvc"}
EFFICIENT_CODEC_HEADROOM = 1.8
MIN_RATE_KBPS = 300  # a ceiling below this only makes the picture fall apart
HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}  # PQ (HDR10) and HLG

# HDR → SDR chains. The first needs ffmpeg with libzimg, the second a recent swscale.
HDR_FILTER_ZSCALE = (
    "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
    "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv"
)
HDR_FILTER_SWSCALE = (
    "scale=out_transfer=linear:out_primaries=bt709,format=gbrpf32le,"
    "tonemap=tonemap=hable:desat=0,"
    "scale=out_transfer=bt709:out_color_matrix=bt709:out_range=tv"
)

# The projector check: the projector plays a generated clip for every resolution and frame
# rate offered on the settings page and reports how it went, and measures its download speed.
TVTEST_VERSION = 1  # part of the clip file names: raise it when the clips change
TVTEST_CLIP_SECONDS = 8
TVTEST_MEASURE_SECONDS = 6  # how long playback of each clip is watched
TVTEST_SPEED_SECONDS = 6
TVTEST_SPEED_CHUNK = 4 * MB
TVTEST_MIN_ADVANCE = 0.9  # playback must keep up with the clock this closely
TVTEST_MAX_DROPPED = 0.05  # and drop at most this share of the frames
TVTEST_STALE_SECONDS = 120  # a projector silent for this long has given up
TVTEST_CLIP_TIMEOUT_SECONDS = 5 * 60
# A sensible bitrate ceiling for each resolution: the settings page suggests it, and the
# clips of the projector check are encoded at it.
SUGGESTED_KBPS = {720: 4000, 1080: 8000, 1440: 16000, 2160: 30000}
# The bitrate ceiling should leave this much of the measured speed for everything else.
NETWORK_SHARE = 0.5


@dataclass
class Settings:
    data_dir: Path
    token: str = ""
    port: int = 8080
    max_upload_bytes: int = 4096 * MB
    # How much free disk space must remain after an upload.
    disk_reserve_bytes: int = 500 * MB
    max_bitrate_kbps: int = 8000
    x264_preset: str = "veryfast"

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        return cls(
            data_dir=Path(env.get("PROMIN_DATA_DIR", "./data")).resolve(),
            token=env.get("PROMIN_TOKEN", "").strip(),
            port=int(env.get("PROMIN_PORT", "8080")),
            max_upload_bytes=int(env.get("PROMIN_MAX_UPLOAD_MB", "4096")) * MB,
            max_bitrate_kbps=int(env.get("PROMIN_MAX_BITRATE_KBPS", "8000")),
            x264_preset=env.get("PROMIN_X264_PRESET", "veryfast"),
        )

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def videos_dir(self) -> Path:
        return self.data_dir / "videos"

    @property
    def thumbs_dir(self) -> Path:
        return self.data_dir / "thumbs"

    @property
    def tvtest_dir(self) -> Path:
        return self.data_dir / "tvtest"


def _sibling(path: Path, suffix: str) -> Path:
    return path.with_name(path.name + suffix)


def write_json(path: Path, data: Any, private: bool = False) -> None:
    """Writes a data file so that a power cut leaves a readable version behind.

    The new text reaches the disk before it replaces the file, and the previous
    version stays next to it as `.bak`.
    """
    tmp = _sibling(path, ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    if private:
        os.chmod(tmp, 0o600)
    if path.exists():
        os.replace(path, _sibling(path, ".bak"))
    os.replace(tmp, path)
    with suppress(OSError):  # make the renames themselves durable where the system allows
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def read_json(path: Path, check: Callable[[Any], Any]) -> Any:
    """Reads a data file written by write_json; None if there is nothing usable.

    `check` turns the parsed JSON into what the store keeps, raising ValueError,
    TypeError, KeyError or AttributeError if it does not fit. A damaged file is moved
    aside as `.broken-<time>` rather than overwritten, and the newest readable copy
    (an unfinished `.tmp`, then the `.bak`) is used instead.
    """
    if path.exists():
        try:
            return check(json.loads(path.read_text(encoding="utf-8")))
        except (ValueError, TypeError, KeyError, AttributeError):
            broken = _sibling(path, ".broken-%d" % time.time())
            os.replace(path, broken)
            log.error("%s is damaged; kept as %s", path.name, broken.name)
    for copy in (_sibling(path, ".tmp"), _sibling(path, ".bak")):
        if not copy.exists():
            continue
        try:
            data = check(json.loads(copy.read_text(encoding="utf-8")))
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
        log.warning("%s could not be read; using %s", path.name, copy.name)
        return data
    return None


def dict_of_dicts(data: Any) -> Dict[str, Dict[str, Any]]:
    if not isinstance(data, dict) or not all(isinstance(v, dict) for v in data.values()):
        raise TypeError("expected an object of objects")
    return data


def records(*keys: str) -> Callable[[Any], Dict[str, Dict[str, Any]]]:
    """A check for read_json: an object of objects that all have these keys.

    The stores read these keys without looking, so a file that parses but lacks one
    counts as damaged instead of failing later, in the middle of a request or a start.
    """

    def check(data: Any) -> Dict[str, Dict[str, Any]]:
        for name, value in dict_of_dicts(data).items():
            missing = [key for key in keys if key not in value]
            if missing:
                raise KeyError("%s has no %s" % (name, ", ".join(missing)))
        return data

    return check


def video_records(data: Any) -> Dict[str, Dict[str, Any]]:
    for item in records("id", "name", "status", "created")(data).values():
        # The list is sorted by this, and bool is a number to Python.
        if isinstance(item["created"], bool) or not isinstance(item["created"], (int, float)):
            raise TypeError("created is not a number")
    return data


@dataclass(frozen=True)
class Encoding:
    """How newly uploaded videos are encoded. Videos that are already processed stay as is."""

    max_height: int = 1080
    max_fps: int = 60
    max_bitrate_kbps: int = 8000
    crf: int = 21
    audio_bitrate_kbps: int = 160
    x264_preset: str = "veryfast"

    @property
    def max_width(self) -> int:
        return HEIGHTS[self.max_height]

    def problem(self) -> Optional[str]:
        """Why these values cannot be used, as text for the user; None if they can."""
        if self.max_height not in HEIGHTS:
            return "Невідома роздільність"
        if self.max_fps not in FPS_CHOICES:
            return "Невідома частота кадрів"
        if not BITRATE_RANGE_KBPS[0] <= self.max_bitrate_kbps <= BITRATE_RANGE_KBPS[1]:
            return "Бітрейт відео має бути від %d до %d кбіт/с" % BITRATE_RANGE_KBPS
        if not CRF_RANGE[0] <= self.crf <= CRF_RANGE[1]:
            return "Якість (CRF) має бути від %d до %d" % CRF_RANGE
        if self.audio_bitrate_kbps not in AUDIO_BITRATES_KBPS:
            return "Невідомий бітрейт звуку"
        if self.x264_preset not in X264_PRESETS:
            return "Невідомий пресет"
        return None


class EncodingStore:
    """Encoding chosen on the settings page; until then, the defaults from the environment."""

    def __init__(self, path: Path, defaults: Encoding) -> None:
        self.path = path
        self.defaults = defaults
        self._lock = threading.Lock()
        self._current = defaults

    def load(self) -> None:
        def check(saved: Any) -> Encoding:
            merged = Encoding(**{**asdict(self.defaults), **dict(saved)})
            if merged.problem():
                raise ValueError(merged.problem())
            return merged

        with self._lock:
            # A damaged or outdated file must not stop the service: fall back to the defaults.
            self._current = read_json(self.path, check) or self.defaults

    def get(self) -> Encoding:
        with self._lock:
            return self._current

    def set(self, encoding: Encoding) -> None:
        with self._lock:
            write_json(self.path, asdict(encoding))
            self._current = encoding


class Store:
    """Video list in a JSON file. Writes are locked and atomic."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._items: Dict[str, Dict[str, Any]] = {}

    def load(self) -> None:
        with self._lock:
            self._items = read_json(self.path, video_records) or {}

    def _save(self) -> None:
        write_json(self.path, self._items)

    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            items = [dict(item) for item in self._items.values()]
        return sorted(items, key=lambda item: item["created"], reverse=True)

    def get(self, video_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            item = self._items.get(video_id)
            return dict(item) if item else None

    def add(self, item: Dict[str, Any]) -> None:
        with self._lock:
            self._items[item["id"]] = item
            self._save()

    def update(self, video_id: str, **fields: Any) -> bool:
        with self._lock:
            if video_id not in self._items:
                return False
            self._items[video_id].update(fields)
            self._save()
            return True

    def remove(self, video_id: str) -> bool:
        return bool(self.remove_many([video_id]))

    def remove_many(self, video_ids: List[str]) -> List[str]:
        """Removes these videos with a single write; returns the ids that were there."""
        with self._lock:
            removed = [video_id for video_id in video_ids if video_id in self._items]
            for video_id in removed:
                del self._items[video_id]
            if removed:
                self._save()
            return removed

    def clear_folder(self, folder_id: str) -> None:
        """Leaves the videos of a folder without one, with a single write."""
        with self._lock:
            inside = [item for item in self._items.values() if item.get("folder") == folder_id]
            for item in inside:
                item["folder"] = None
            if inside:
                self._save()


# ------------------------------------------------------------------ transcoding


class ProcessingError(Exception):
    """A processing error whose text is shown to the user."""


def parse_fps(value: Optional[str]) -> float:
    try:
        num, _, den = (value or "").partition("/")
        return float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        return 0.0


def stream_rotation(stream: Dict[str, Any]) -> int:
    for side in stream.get("side_data_list") or []:
        if "rotation" in side:
            return int(round(float(side["rotation"]))) % 360
    try:
        return int(float((stream.get("tags") or {}).get("rotate", 0))) % 360
    except ValueError:
        return 0


def plan_encode(probe: Dict[str, Any], encoding: Encoding) -> Dict[str, Any]:
    """Decides from the ffprobe output how much work is needed.

    remux: the file already fits, only repackage it;
    audio: the video fits, re-encode only the audio;
    transcode: re-encode everything.
    """
    streams = probe.get("streams") or []
    video = next(
        (
            s
            for s in streams
            if s.get("codec_type") == "video"
            and not (s.get("disposition") or {}).get("attached_pic")
        ),
        None,
    )
    if video is None:
        raise ProcessingError("У файлі немає відеодоріжки")
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = probe.get("format") or {}

    def number(*values: Any) -> float:
        for value in values:
            try:
                if float(value) > 0:
                    return float(value)
            except (TypeError, ValueError):
                continue
        return 0.0

    duration = number(fmt.get("duration"), video.get("duration"))
    fps = parse_fps(video.get("avg_frame_rate")) or parse_fps(video.get("r_frame_rate"))
    bitrate = number(video.get("bit_rate"), fmt.get("bit_rate"))
    hdr = video.get("color_transfer") in HDR_TRANSFERS

    # Leave the video alone only when the projector is sure to play it as is.
    video_ok = (
        video.get("codec_name") == "h264"
        and video.get("pix_fmt") == "yuv420p"
        and not hdr
        and stream_rotation(video) == 0
        and 0 < int(video.get("width") or 0) <= encoding.max_width
        and 0 < int(video.get("height") or 0) <= encoding.max_height
        and 0 < fps <= encoding.max_fps + 0.5
        and 0 < bitrate <= encoding.max_bitrate_kbps * 1000 * 1.25
    )
    # Keep the audio as is if it is mono or stereo AAC.
    audio_ok = audio is None or (
        audio.get("codec_name") == "aac" and int(audio.get("channels") or 2) <= 2
    )
    if video_ok and audio_ok:
        mode = "remux"
    elif video_ok:
        mode = "audio"
    else:
        mode = "transcode"
    # A transcoded file gains nothing from more bits than the source had, so the ceiling
    # follows the source bitrate; more efficient codecs get headroom to keep their quality.
    max_rate = encoding.max_bitrate_kbps
    if bitrate > 0:
        headroom = EFFICIENT_CODEC_HEADROOM if video.get("codec_name") in EFFICIENT_CODECS else 1.0
        max_rate = min(max_rate, max(MIN_RATE_KBPS, int(bitrate / 1000 * headroom)))
    return {
        "mode": mode,
        "duration": duration,
        "fps": fps,
        "hdr": hdr,
        "max_rate_kbps": max_rate,
    }


def video_info(probe: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """What the finished file is, for the card: frame size, frame rate, bitrate, codecs."""
    streams = probe.get("streams") or []
    video = next(
        (
            s for s in streams
            if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")
        ),
        None,
    )
    if video is None:
        return None
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fps = parse_fps(video.get("avg_frame_rate")) or parse_fps(video.get("r_frame_rate"))
    bitrate = 0.0
    for value in (video.get("bit_rate"), (probe.get("format") or {}).get("bit_rate")):
        try:
            bitrate = float(value)
            break
        except (TypeError, ValueError):
            continue
    try:
        width, height = int(video.get("width") or 0), int(video.get("height") or 0)
        channels = int(audio.get("channels") or 0) if audio else 0
    except (TypeError, ValueError):
        return None
    return {
        "width": width,
        "height": height,
        "fps": round(fps, 2),
        "bitrate_kbps": int(bitrate / 1000),
        "video_codec": video.get("codec_name"),
        "audio_codec": audio.get("codec_name") if audio else None,
        "channels": channels,
    }


def build_ffmpeg_args(
    ffmpeg: str,
    src: Path,
    dst: Path,
    plan: Dict[str, Any],
    encoding: Encoding,
    hdr_filter: Optional[str],
) -> List[str]:
    args = [ffmpeg, "-nostdin", "-y", "-v", "error", "-i", str(src)]
    args += ["-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn"]
    audio_rate = "%dk" % encoding.audio_bitrate_kbps
    if plan["mode"] == "remux":
        args += ["-c", "copy"]
    elif plan["mode"] == "audio":
        # The picture is copied losslessly; only the audio is re-encoded.
        args += ["-c:v", "copy", "-c:a", "aac", "-b:a", audio_rate, "-ac", "2"]
    else:
        tonemapped = bool(plan["hdr"] and hdr_filter)
        # Fit into the frame without upscaling; ffmpeg applies rotation by itself.
        filters = [
            "scale=w='min(%d,iw)':h='min(%d,ih)'"
            ":force_original_aspect_ratio=decrease:force_divisible_by=2"
            % (encoding.max_width, encoding.max_height)
        ]
        # Tone mapping goes after downscaling: on 4K it is three times slower.
        if tonemapped:
            filters.append(hdr_filter)
        if plan["fps"] > encoding.max_fps + 0.5:
            filters.append("fps=%d" % encoding.max_fps)
        filters.append("format=yuv420p")
        rate = plan["max_rate_kbps"]
        args += [
            "-vf", ",".join(filters),
            "-c:v", "libx264",
            "-preset", encoding.x264_preset,
            "-profile:v", "high",
            "-crf", str(encoding.crf),
            "-maxrate", "%dk" % rate,
            "-bufsize", "%dk" % (rate * 2),
            # A keyframe every 2 seconds, for fast seeking.
            "-force_key_frames", "expr:gte(t,n_forced*2)",
            "-c:a", "aac", "-b:a", audio_rate, "-ac", "2",
        ]
        if tonemapped:
            args += ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"]
    # Strip metadata: phone videos often carry a location.
    args += ["-map_metadata", "-1", "-movflags", "+faststart"]
    args += ["-progress", "pipe:1", "-nostats", str(dst)]
    return args


def detect_hdr_filter(ffmpeg: str) -> Optional[str]:
    """Which HDR → SDR chain the installed ffmpeg supports."""

    def output(*args: str) -> str:
        try:
            result = subprocess.run(
                [ffmpeg, "-hide_banner", *args], capture_output=True, text=True, timeout=20
            )
            return result.stdout
        except (OSError, subprocess.SubprocessError):
            return ""

    filters = output("-filters")
    if " tonemap " not in filters:
        return None
    if " zscale " in filters:
        return HDR_FILTER_ZSCALE
    if "out_transfer" in output("-h", "filter=scale"):
        return HDR_FILTER_SWSCALE
    return None


class Transcoder:
    """A queue with a single worker: videos are processed one at a time."""

    def __init__(self, settings: Settings, store: Store, encoding: EncodingStore) -> None:
        self.settings = settings
        self.store = store
        self.encoding = encoding
        self.ffmpeg = shutil.which("ffmpeg")
        self.ffprobe = shutil.which("ffprobe")
        self.hdr_filter: Optional[str] = None
        self.stall_seconds = FFMPEG_STALL_SECONDS
        self.progress: Dict[str, float] = {}
        self._queue: Optional[asyncio.Queue] = None
        self._task: Optional[asyncio.Task] = None
        self._thumb_task: Optional[asyncio.Task] = None
        self._current_id: Optional[str] = None
        self._current_proc: Optional[asyncio.subprocess.Process] = None

    async def start(self) -> None:
        self._queue = asyncio.Queue()
        if self.ffmpeg:
            self.hdr_filter = await asyncio.to_thread(detect_hdr_filter, self.ffmpeg)
            if self.hdr_filter is None:
                log.warning("ffmpeg cannot tone map: HDR videos may look washed out")
        else:
            log.warning("ffmpeg not found: videos will not be processed")
        # Jobs that did not finish before a restart go back to the queue.
        pending = [
            item for item in self.store.list() if item["status"] in ("queued", "processing")
        ]
        for item in sorted(pending, key=lambda item: item["created"]):
            self.store.update(item["id"], status="queued", error=None)
            self.enqueue(item["id"])
        self._task = asyncio.create_task(self._run())
        self._thumb_task = asyncio.create_task(self._maintenance())

    async def stop(self) -> None:
        for task in (self._thumb_task, self._task):
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    async def _maintenance(self) -> None:
        """Brings videos processed by older versions up to date, one at a time."""
        await self._backfill_info()
        await self._refresh_thumbnails()

    async def _backfill_info(self) -> None:
        """Reads the quality of videos processed before it was recorded."""
        if not self.ffprobe:
            return
        for item in self.store.list():
            if item["status"] != "ready" or item.get("info"):
                continue
            video = self.settings.videos_dir / (item["id"] + ".mp4")
            if not video.is_file():
                continue
            info = await self._info(video)
            if info:
                self.store.update(item["id"], info=info)

    async def _info(self, video: Path) -> Optional[Dict[str, Any]]:
        try:
            return video_info(await self._probe(video))
        except asyncio.CancelledError:
            raise
        except Exception:  # the card simply shows no quality
            log.exception("Could not read the quality of %s", video.name)
            return None

    async def _refresh_thumbnails(self) -> None:
        """Remakes thumbnails made the old way (often a black frame)."""
        if not self.ffmpeg:
            return
        for item in self.store.list():
            if item["status"] != "ready" or item.get("thumb_v") == THUMB_VERSION:
                continue
            video = self.settings.videos_dir / (item["id"] + ".mp4")
            if not video.is_file():
                continue
            try:
                made = await self._thumbnail(video, item["id"], item.get("duration") or 0)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Could not remake the thumbnail for %s", item["id"])
                continue
            self.store.update(
                item["id"], thumb=made or bool(item.get("thumb")), thumb_v=THUMB_VERSION
            )

    def enqueue(self, video_id: str) -> None:
        assert self._queue is not None
        self._queue.put_nowait(video_id)

    def cancel(self, video_id: str) -> None:
        """Stops ffmpeg if it is processing this video right now."""
        proc = self._current_proc
        if self._current_id == video_id and proc and proc.returncode is None:
            proc.kill()

    async def _run(self) -> None:
        assert self._queue is not None
        while True:
            video_id = await self._queue.get()
            try:
                await self._process(video_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # one video must not bring the worker down
                log.exception("Could not process %s", video_id)
                self.store.update(video_id, status="error", error=str(exc)[-2000:])

    async def _process(self, video_id: str) -> None:
        item = self.store.get(video_id)
        if not item or item["status"] != "queued":
            return
        settings = self.settings
        # A record without a source (a hand-edited list) fails below as a missing original.
        src = settings.uploads_dir / (item.get("source") or "missing")
        tmp = settings.videos_dir / (video_id + ".tmp.mp4")
        final = settings.videos_dir / (video_id + ".mp4")
        self._current_id = video_id
        self.progress[video_id] = 0.0
        self.store.update(video_id, status="processing")
        try:
            if not self.ffmpeg or not self.ffprobe:
                raise ProcessingError("На сервері не встановлено ffmpeg")
            if not src.is_file():
                raise ProcessingError("Оригінал відео не знайдено на сервері")

            # The settings in force when processing starts apply to the whole video.
            encoding = self.encoding.get()
            plan = plan_encode(await self._probe(src), encoding)
            args = build_ffmpeg_args(self.ffmpeg, src, tmp, plan, encoding, self.hdr_filter)
            code, err = await self._ffmpeg(args, video_id, plan["duration"])

            if self.store.get(video_id) is None:  # deleted while processing
                remove_files(settings, video_id)
                return
            if code != 0:
                raise ProcessingError(err.strip() or "ffmpeg завершився з кодом %d" % code)

            os.replace(tmp, final)
            thumb = await self._thumbnail(final, video_id, plan["duration"])
            info = await self._info(final)
            updated = self.store.update(
                video_id,
                status="ready",
                size=final.stat().st_size,
                duration=plan["duration"] or None,
                thumb=thumb,
                thumb_v=THUMB_VERSION,
                info=info,
                error=None,
            )
            if not updated:
                remove_files(settings, video_id)
                return
            src.unlink(missing_ok=True)  # the original stays on the phone
        except ProcessingError as exc:
            # Server paths mean nothing to the user.
            text = str(exc).replace(str(src), item["name"]).replace(str(tmp), "готовий файл")
            self.store.update(video_id, status="error", error=text[-2000:])
        finally:
            proc = self._current_proc
            if proc and proc.returncode is None:
                proc.kill()
            # After any failure, an unexpected one included; on success it is already renamed.
            tmp.unlink(missing_ok=True)
            self._current_id = None
            self._current_proc = None
            self.progress.pop(video_id, None)

    async def _probe(self, src: Path) -> Dict[str, Any]:
        proc = await asyncio.create_subprocess_exec(
            self.ffprobe, "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(src),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), PROBE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            raise ProcessingError("ffprobe не відповів за %d хвилини" % (PROBE_TIMEOUT_SECONDS // 60))
        finally:
            await self._reap(proc)
        if proc.returncode != 0:
            detail = " ".join(err.decode(errors="replace").split())
            raise ProcessingError("Файл не вдалося прочитати як відео (%s)" % detail)
        return json.loads(out)

    async def _ffmpeg(self, args: List[str], video_id: str, duration: float) -> Tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        self._current_proc = proc
        assert proc.stdout is not None and proc.stderr is not None
        err_task = asyncio.create_task(proc.stderr.read())
        stalled = False
        try:
            while True:
                # ffmpeg reports progress twice a second; a long silence means it is stuck.
                try:
                    raw = await asyncio.wait_for(proc.stdout.readline(), self.stall_seconds)
                except asyncio.TimeoutError:
                    stalled = True
                    proc.kill()
                    break
                if not raw:
                    break
                key, _, value = raw.decode(errors="replace").strip().partition("=")
                # ffmpeg's out_time_ms is in microseconds too, a historical quirk.
                if key in ("out_time_us", "out_time_ms") and duration > 0:
                    with suppress(ValueError):
                        done = int(value) / 1_000_000 / duration
                        self.progress[video_id] = max(0.0, min(done, 1.0))
            code = await proc.wait()
            err = (await err_task).decode(errors="replace")
            if stalled:
                err = "ffmpeg завис: %d хвилин без жодного поступу" % (self.stall_seconds // 60)
                code = code or -1
            return code, err
        finally:
            err_task.cancel()
            await self._reap(proc)

    @staticmethod
    async def _reap(proc: asyncio.subprocess.Process) -> None:
        """Does not leave the process running if processing was interrupted."""
        if proc.returncode is None:
            proc.kill()
            await proc.wait()

    async def _thumbnail(self, video: Path, video_id: str, duration: float) -> bool:
        out = self.settings.thumbs_dir / (video_id + ".jpg")
        tmp = self.settings.thumbs_dir / (video_id + ".tmp.jpg")
        proc = await asyncio.create_subprocess_exec(
            *thumbnail_args(self.ffmpeg, video, tmp, duration),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.wait(), THUMB_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            log.warning("Thumbnail for %s took too long", video_id)
        finally:
            await self._reap(proc)
        if proc.returncode != 0 or not tmp.is_file():
            tmp.unlink(missing_ok=True)
            return False
        os.replace(tmp, out)
        return True


def thumbnail_args(ffmpeg: str, video: Path, out: Path, duration: float) -> List[str]:
    """Thumbnail frame: not from the very start, which is often black or a title card."""
    if duration > 20:
        # Take the most representative frame of two seconds, a tenth of the way in.
        seek, filters = duration * 0.1, "thumbnail=50,scale=640:-2"
    else:
        seek, filters = min(1.0, duration / 2), "scale=640:-2"
    return [
        ffmpeg, "-nostdin", "-y", "-v", "error",
        "-ss", "%.2f" % seek, "-i", str(video),
        "-frames:v", "1", "-vf", filters, "-q:v", "5", "-f", "image2", str(out),
    ]


# ------------------------------------------------------------------------ login


class NotAuthenticated(Exception):
    pass


class LoginLimiter:
    """Limits failed login attempts from one address."""

    def __init__(self, max_failures: int = LOGIN_MAX_FAILURES, window: float = LOGIN_WINDOW_SECONDS) -> None:
        self.max_failures = max_failures
        self.window = window
        self._failures: Dict[str, List[float]] = {}

    def _recent(self, ip: str) -> List[float]:
        cutoff = time.monotonic() - self.window
        recent = [t for t in self._failures.get(ip, []) if t > cutoff]
        if recent:
            self._failures[ip] = recent
        else:
            self._failures.pop(ip, None)
        return recent

    def blocked(self, ip: str) -> bool:
        return len(self._recent(ip)) >= self.max_failures

    def fail(self, ip: str) -> float:
        """Records an attempt and returns its stamp, by which it can be forgiven."""
        stamp = time.monotonic()
        if len(self._failures) >= LIMITER_MAX_ADDRESSES:
            # Addresses that never return are only dropped here, so the table stays bounded.
            for known in list(self._failures):
                self._recent(known)
        self._failures.setdefault(ip, []).append(stamp)
        return stamp

    def forgive(self, ip: str, stamp: float) -> None:
        """Forgives one attempt (it turned out successful); other failures stay."""
        attempts = self._failures.get(ip)
        if attempts and stamp in attempts:
            attempts.remove(stamp)
            if not attempts:
                del self._failures[ip]


def same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS).hex()


class UserStore:
    """Users in a JSON file; passwords are stored only as hashes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._users: Dict[str, Dict[str, Any]] = {}

    def load(self) -> None:
        with self._lock:
            self._users = read_json(self.path, records("role", "salt", "hash", "created")) or {}

    def _save(self) -> None:
        write_json(self.path, self._users, private=True)

    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                {"username": name, "role": user["role"], "created": user["created"]}
                for name, user in sorted(self._users.items())
            ]

    def get(self, username: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            user = self._users.get(username)
            return dict(user) if user else None

    def add(self, username: str, password: str, role: str) -> bool:
        salt = secrets.token_bytes(16)
        digest = hash_password(password, salt)
        with self._lock:
            if username in self._users:
                return False
            self._users[username] = {
                "role": role, "salt": salt.hex(), "hash": digest, "created": time.time(),
            }
            self._save()
            return True

    def set_password(self, username: str, password: str) -> bool:
        salt = secrets.token_bytes(16)
        digest = hash_password(password, salt)
        with self._lock:
            if username not in self._users:
                return False
            self._users[username].update(salt=salt.hex(), hash=digest)
            self._save()
            return True

    def remove(self, username: str) -> bool:
        with self._lock:
            if username not in self._users:
                return False
            del self._users[username]
            self._save()
            return True


class SessionStore:
    """User sessions. Only a hash of the cookie secret is stored."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._sessions: Dict[str, Dict[str, Any]] = {}

    def load(self) -> None:
        with self._lock:
            check = records("username", "hash", "fingerprint", "seen")
            self._sessions = read_json(self.path, check) or {}

    def _save(self) -> None:
        write_json(self.path, self._sessions, private=True)

    def add(self, username: str, fingerprint: str) -> str:
        """Creates a session and returns its cookie value. Also purges expired ones."""
        session_id = secrets.token_hex(8)
        secret = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            for key in [k for k, v in self._sessions.items() if v["seen"] < now - SESSION_IDLE_TTL]:
                del self._sessions[key]
            self._sessions[session_id] = {
                "username": username,
                "hash": hashlib.sha256(secret.encode("utf-8")).hexdigest(),
                "fingerprint": fingerprint,
                "created": now,
                "seen": now,
            }
            self._save()
        return "%s.%s" % (session_id, secret)

    def get(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            session = self._sessions.get(session_id)
            return dict(session) if session else None

    def touch(self, session_id: str) -> None:
        with self._lock:
            if session_id in self._sessions:
                self._sessions[session_id]["seen"] = time.time()
                self._save()

    def remove(self, session_id: str) -> bool:
        with self._lock:
            if session_id not in self._sessions:
                return False
            del self._sessions[session_id]
            self._save()
            return True


class FolderStore:
    """Folders of the library, one level deep. A video names its folder; a folder lists nothing."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._folders: Dict[str, Dict[str, Any]] = {}

    def load(self) -> None:
        def check(data: Any) -> Dict[str, Dict[str, Any]]:
            return {
                str(key): {"name": str(value["name"]), "created": float(value["created"])}
                for key, value in data.items()
            }

        with self._lock:
            # Videos keep their folder ids, so the folders come back once the file is fixed.
            self._folders = read_json(self.path, check) or {}

    def _save(self) -> None:
        write_json(self.path, self._folders)

    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            items = [{"id": key, "name": f["name"]} for key, f in self._folders.items()]
        return sorted(items, key=lambda f: f["name"].casefold())

    def exists(self, folder_id: Optional[str]) -> bool:
        with self._lock:
            return folder_id in self._folders

    def _taken(self, name: str, other_than: Optional[str] = None) -> bool:
        return any(
            f["name"].casefold() == name.casefold()
            for key, f in self._folders.items() if key != other_than
        )

    def add(self, name: str) -> str:
        """Creates a folder and returns its id; raises ValueError with text for the user."""
        with self._lock:
            if len(self._folders) >= FOLDERS_MAX:
                raise ValueError("Тек може бути щонайбільше %d" % FOLDERS_MAX)
            if self._taken(name):
                raise ValueError("Така тека вже є")
            folder_id = secrets.token_hex(6)
            self._folders[folder_id] = {"name": name, "created": time.time()}
            self._save()
            return folder_id

    def rename(self, folder_id: str, name: str) -> bool:
        """False if there is no such folder; ValueError if the name is taken."""
        with self._lock:
            if folder_id not in self._folders:
                return False
            if self._taken(name, other_than=folder_id):
                raise ValueError("Така тека вже є")
            self._folders[folder_id]["name"] = name
            self._save()
            return True

    def remove(self, folder_id: str) -> bool:
        with self._lock:
            if folder_id not in self._folders:
                return False
            del self._folders[folder_id]
            self._save()
            return True


def clean_folder_name(raw: str) -> str:
    """A folder name as typed, tidied up; ValueError with text for the user if it cannot be one."""
    name = " ".join("".join(ch for ch in raw if ch.isprintable()).split())
    if not name:
        raise ValueError("Назва теки не може бути порожньою")
    if len(name) > FOLDER_NAME_MAX:
        raise ValueError("Назва теки задовга: щонайбільше %d символів" % FOLDER_NAME_MAX)
    return name


class QueueStore:
    """The shared watch queue of the projector: what plays next, in order, in a JSON file.

    Each entry has its own id, so one video can be queued twice and an entry can be
    moved or removed without touching its twin.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._entries: List[Dict[str, str]] = []

    def load(self) -> None:
        def check(data: Any) -> List[Dict[str, str]]:
            if not isinstance(data, list):
                raise TypeError("expected a list")
            return [{"entry": str(e["entry"]), "video_id": str(e["video_id"])} for e in data]

        with self._lock:
            # A lost queue is a small loss; a service that will not start is a big one.
            self._entries = read_json(self.path, check) or []

    def _save(self) -> None:
        write_json(self.path, self._entries)

    def list(self) -> List[Dict[str, str]]:
        with self._lock:
            return [dict(entry) for entry in self._entries]

    def add(self, video_id: str) -> Optional[str]:
        """Appends a video and returns the new entry id; None if the queue is full."""
        with self._lock:
            if len(self._entries) >= QUEUE_MAX:
                return None
            entry = secrets.token_hex(6)
            self._entries.append({"entry": entry, "video_id": video_id})
            self._save()
            return entry

    def remove(self, entry: str) -> bool:
        with self._lock:
            kept = [e for e in self._entries if e["entry"] != entry]
            if len(kept) == len(self._entries):
                return False
            self._entries = kept
            self._save()
            return True

    def remove_videos(self, video_ids: set) -> bool:
        """Drops every entry of these videos. True if the queue changed."""
        with self._lock:
            kept = [e for e in self._entries if e["video_id"] not in video_ids]
            if len(kept) == len(self._entries):
                return False
            self._entries = kept
            self._save()
            return True

    def move(self, entry: str, position: int) -> bool:
        with self._lock:
            index = next((i for i, e in enumerate(self._entries) if e["entry"] == entry), None)
            if index is None:
                return False
            item = self._entries.pop(index)
            position = max(0, min(position, len(self._entries)))
            self._entries.insert(position, item)
            self._save()
            return True

    def clear(self) -> None:
        with self._lock:
            self._entries = []
            self._save()

    def pop(self) -> Optional[Dict[str, str]]:
        with self._lock:
            if not self._entries:
                return None
            entry = self._entries.pop(0)
            self._save()
            return entry


class DeviceStore:
    """Paired projectors. Only a hash of the device cookie secret is stored.

    A projector that has not been in touch for DEVICE_IDLE_TTL is forgotten: its cookie was
    lost (cleared browser, a new pairing) or it is no longer used.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._devices: Dict[str, Dict[str, Any]] = {}

    def load(self) -> None:
        with self._lock:
            check = records("name", "hash", "approved_by", "created")
            self._devices = read_json(self.path, check) or {}
            if self._purge():
                self._save()

    def _save(self) -> None:
        write_json(self.path, self._devices, private=True)

    @staticmethod
    def seen(device: Dict[str, Any]) -> float:
        """When the device was last in touch; one paired before this was recorded counts
        from its pairing."""
        return device.get("seen") or device["created"]

    def _purge(self) -> bool:
        cutoff = time.time() - DEVICE_IDLE_TTL
        stale = [key for key, d in self._devices.items() if self.seen(d) < cutoff]
        for key in stale:
            del self._devices[key]
        return bool(stale)

    def add(self, name: str, approved_by: str) -> str:
        """Creates a device and returns its cookie value."""
        device_id = secrets.token_hex(8)
        secret = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            self._purge()
            self._devices[device_id] = {
                "name": name,
                "hash": hashlib.sha256(secret.encode("utf-8")).hexdigest(),
                "approved_by": approved_by,
                "created": now,
                "seen": now,
            }
            self._save()
        return "@%s.%s" % (device_id, secret)

    def touch(self, device_id: str) -> None:
        with self._lock:
            if device_id in self._devices:
                self._devices[device_id]["seen"] = time.time()
                self._save()

    def get(self, device_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            device = self._devices.get(device_id)
            return dict(device) if device else None

    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            if self._purge():
                self._save()
            items = [
                {
                    "id": key, "name": d["name"], "approved_by": d["approved_by"],
                    "created": d["created"], "seen": self.seen(d),
                }
                for key, d in self._devices.items()
            ]
        return sorted(items, key=lambda item: item["created"])

    def remove(self, device_id: str) -> bool:
        with self._lock:
            if device_id not in self._devices:
                return False
            del self._devices[device_id]
            self._save()
            return True


class Pairing:
    """Projectors that are showing a QR code right now. They live in memory only.

    Each has two secrets. The polling token stays with the projector; the approval key goes
    into the QR code on its screen. So someone who sees the screen can approve the projector
    (after logging in) but cannot collect the cookie meant for it.
    """

    def __init__(self) -> None:
        self.pending: Dict[str, Dict[str, Any]] = {}  # polling token → entry

    def purge(self) -> None:
        cutoff = time.monotonic() - PAIR_CODE_TTL
        for token in [t for t, entry in self.pending.items() if entry["created"] < cutoff]:
            del self.pending[token]

    def start(self) -> Optional[Dict[str, Any]]:
        self.purge()
        if len(self.pending) >= PAIR_MAX_PENDING:
            return None
        token = secrets.token_urlsafe(24)
        key = secrets.token_urlsafe(24)
        self.pending[token] = {"key": key, "created": time.monotonic(), "approved_by": None}
        return {"key": key, "token": token, "expires_in": PAIR_CODE_TTL}

    def by_key(self, key: str) -> Optional[Dict[str, Any]]:
        self.purge()
        for entry in self.pending.values():
            if entry["approved_by"] is None and same(entry["key"], key):
                return entry
        return None


class PairToken(BaseModel):
    token: str


class PairKey(BaseModel):
    key: str


class NewUser(BaseModel):
    username: str
    password: str
    role: str = "viewer"


class NewPassword(BaseModel):
    password: str


class VideoIds(BaseModel):
    ids: List[str]


class FolderName(BaseModel):
    name: str


class VideoFolder(BaseModel):
    folder: Optional[str] = None


class QueueAdd(BaseModel):
    video_id: str


class QueueMove(BaseModel):
    position: int


class EncodingBody(BaseModel):
    max_height: int
    max_fps: int
    max_bitrate_kbps: int
    crf: int
    audio_bitrate_kbps: int
    x264_preset: str


# ----------------------------------------------------------------------- remote


class Hub:
    """Connects remotes to the projector. State lives in the memory of one process."""

    def __init__(self) -> None:
        self.tv: Optional[WebSocket] = None
        self.remotes: set = set()
        self.state: Optional[Dict[str, Any]] = None

    @staticmethod
    async def send(ws: WebSocket, message: Dict[str, Any]) -> None:
        with suppress(Exception):  # the connection may have just dropped
            await ws.send_json(message)

    async def broadcast(self, message: Dict[str, Any]) -> None:
        for ws in list(self.remotes):
            await self.send(ws, message)


def finite(value: Any) -> float:
    """A number from the projector page; anything else, NaN and infinity read as 0."""
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return number if number == number and abs(number) != float("inf") else 0.0


def clean_state(message: Dict[str, Any]) -> Dict[str, Any]:
    """State from the projector page: only known fields of known types."""

    def number(key: str) -> float:
        return finite(message.get(key))

    video_id = message.get("video_id")
    name = message.get("name")
    return {
        "video_id": str(video_id)[:64] if video_id else None,
        "name": str(name)[:200] if name else None,
        "position": number("position"),
        "duration": number("duration"),
        "volume": max(0.0, min(number("volume"), 1.0)),
        "paused": bool(message.get("paused")),
        "started": bool(message.get("started")),
    }


# -------------------------------------------------------------- projector check


def tvtest_levels() -> List[Dict[str, Any]]:
    """The clips of the projector check, easiest first.

    `needs` names the clip that has to play smoothly before this one is worth trying: the
    same resolution at the lower frame rate, or the resolution below.
    """
    levels: List[Dict[str, Any]] = []
    base_fps = min(FPS_CHOICES)
    lower = None
    for height in sorted(HEIGHTS):
        base = "%dp%d" % (height, base_fps)
        for fps in sorted(FPS_CHOICES):
            levels.append({
                "id": "%dp%d" % (height, fps), "height": height, "fps": fps,
                "kbps": SUGGESTED_KBPS[height], "needs": lower if fps == base_fps else base,
            })
        lower = base
    return levels


def tvtest_clip_name(level: Dict[str, Any]) -> str:
    return "v%d-%s.mp4" % (TVTEST_VERSION, level["id"])


def tvtest_clip_args(ffmpeg: str, level: Dict[str, Any], out: Path) -> List[str]:
    """A test clip encoded the way real videos are: H.264 High at the bitrate the settings
    page suggests for this resolution, with an audio track.

    The noise keeps the encoder from spending far fewer bits than asked on a simple
    picture, so the decoder gets a realistic load. The audio is silence, but it is there:
    players drop frames to stay in sync with it.
    """
    rate = level["kbps"]
    return [
        ffmpeg, "-nostdin", "-y", "-v", "error",
        "-f", "lavfi", "-i", "testsrc2=size=%dx%d:rate=%d" % (
            HEIGHTS[level["height"]], level["height"], level["fps"]),
        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
        "-t", str(TVTEST_CLIP_SECONDS),
        "-vf", "noise=alls=12:allf=t,format=yuv420p",
        "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high",
        "-b:v", "%dk" % rate, "-maxrate", "%dk" % rate, "-bufsize", "%dk" % (rate * 2),
        "-g", str(level["fps"] * 2),
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", "-f", "mp4", str(out),
    ]


async def make_tvtest_clip(ffmpeg: str, level: Dict[str, Any], folder: Path) -> None:
    """Generates the clip of this level unless it is already there."""
    final = folder / tvtest_clip_name(level)
    if final.is_file():
        return
    tmp = _sibling(final, ".tmp")
    proc = await asyncio.create_subprocess_exec(
        *tvtest_clip_args(ffmpeg, level, tmp),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, err = await asyncio.wait_for(proc.communicate(), TVTEST_CLIP_TIMEOUT_SECONDS)
        if proc.returncode != 0:
            raise ProcessingError(err.decode(errors="replace").strip()[-2000:])
        os.replace(tmp, final)
    finally:
        await Transcoder._reap(proc)
        tmp.unlink(missing_ok=True)


def clean_tvtest_report(message: Dict[str, Any], levels: List[Dict[str, Any]]) -> Dict[str, Any]:
    """What the projector reported, as known fields of known types, with a verdict for
    every clip: ok (smooth), rough (played, but did not keep up), failed or skipped."""
    reports: Dict[str, Dict[str, Any]] = {}
    raw = message.get("clips")
    for entry in raw if isinstance(raw, list) else []:
        if isinstance(entry, dict):
            reports[str(entry.get("id"))] = entry
    clips = []
    for level in levels:
        entry = reports.get(level["id"], {})
        advance = max(0.0, min(finite(entry.get("advance")), 2.0))
        frames = max(0, int(finite(entry.get("frames"))))
        dropped = max(0, min(int(finite(entry.get("dropped"))), frames))
        clip = {key: level[key] for key in ("id", "height", "fps", "kbps")}
        if entry.get("outcome") == "played":
            # Without frame counters (frames is 0) only the clock is left to judge by.
            smooth = advance >= TVTEST_MIN_ADVANCE and dropped <= frames * TVTEST_MAX_DROPPED
            clip.update(
                status="ok" if smooth else "rough",
                advance=round(advance, 3), frames=frames, dropped=dropped,
            )
        elif entry.get("outcome") == "failed":
            reason = "download" if entry.get("reason") == "download" else "play"
            clip.update(status="failed", reason=reason)
        else:
            clip.update(status="skipped")
        clips.append(clip)
    device = message.get("device")
    device = device if isinstance(device, dict) else {}
    speed = int(finite(message.get("speed_kbps")))
    return {
        "clips": clips,
        "speed_kbps": speed if speed > 0 else None,
        "device": {
            "agent": str(device.get("agent") or "")[:300],
            "screen_width": max(0, int(finite(device.get("screen_width")))),
            "screen_height": max(0, int(finite(device.get("screen_height")))),
        },
    }


def tvtest_fits(result: Dict[str, Any]) -> Dict[str, Any]:
    """What the check says about the settings: the highest resolution and frame rate that
    played smoothly (None if nothing did) and the bitrate ceiling the network allows
    (None if the speed is unknown)."""
    smooth = {clip["id"] for clip in result["clips"] if clip["status"] == "ok"}
    base_fps = min(FPS_CHOICES)
    height = None
    for candidate in sorted(HEIGHTS):
        if "%dp%d" % (candidate, base_fps) not in smooth:
            break
        height = candidate
    fits: Dict[str, Any] = {"max_height": height, "max_fps": None, "network_kbps": None}
    if height is not None:
        fits["max_fps"] = max(f for f in FPS_CHOICES if "%dp%d" % (height, f) in smooth)
    if result.get("speed_kbps"):
        fits["network_kbps"] = int(result["speed_kbps"] * NETWORK_SHARE)
    return fits


def tvtest_record(data: Any) -> Dict[str, Any]:
    """A check for read_json: a finished projector check as TvTest.finish saved it."""
    parts = [(data, ("clips", "device", "fits", "finished", "speed_kbps"))]
    parts += [(clip, ("id", "height", "fps", "status")) for clip in data["clips"]]
    parts += [(data["device"], ("agent",)), (data["fits"], ("max_height", "max_fps"))]
    for part, keys in parts:
        missing = [key for key in keys if key not in part]
        if missing:
            raise KeyError(", ".join(missing))
    return data


class TvTest:
    """The projector check: where a running one is, and the last finished one on disk."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.levels = tvtest_levels()
        self.result: Optional[Dict[str, Any]] = None
        self.status = "idle"  # idle → preparing (clips are generated) → running → idle
        self.run: Optional[str] = None
        self.step = 0
        self.error: Optional[str] = None
        self.seen = 0.0  # when the projector last reported
        self.task: Optional[asyncio.Task] = None

    def load(self) -> None:
        self.result = read_json(self.path, tvtest_record)

    def begin(self) -> str:
        self.status, self.run, self.step, self.error = "preparing", secrets.token_hex(8), 0, None
        return self.run

    def fail(self, error: str) -> None:
        self.status, self.run, self.error = "idle", None, error

    def finish(self, message: Dict[str, Any]) -> None:
        result = clean_tvtest_report(message, self.levels)
        result["fits"] = tvtest_fits(result)
        result["finished"] = time.time()
        write_json(self.path, result)
        self.result = result
        self.status, self.run, self.error = "idle", None, None

    def view(self) -> Dict[str, Any]:
        if self.status == "running" and time.time() - self.seen > TVTEST_STALE_SECONDS:
            self.fail("Проектор перестав відповідати під час перевірки")
        return {
            "status": self.status, "step": self.step, "steps": len(self.levels),
            "error": self.error, "result": self.result,
        }


# ------------------------------------------------------------------ application


def clean_name(raw: str) -> str:
    """Display name: no path, limited length."""
    name = unquote(raw).replace("\\", "/").split("/")[-1].strip()
    return name[:200] or "video"


def remove_files(settings: Settings, video_id: str) -> None:
    for folder in (settings.uploads_dir, settings.videos_dir, settings.thumbs_dir):
        for path in folder.glob(video_id + ".*"):
            path.unlink(missing_ok=True)


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or Settings.from_env()
    if len(settings.token) < MIN_TOKEN_LENGTH:
        raise RuntimeError(
            "Set PROMIN_TOKEN: at least %d characters" % MIN_TOKEN_LENGTH
        )
    # Key for session fingerprints; the token itself never reaches a cookie or a file.
    secret = hmac.new(settings.token.encode("utf-8"), b"promin-session", "sha256").digest()
    users = UserStore(settings.data_dir / "users.json")
    sessions = SessionStore(settings.data_dir / "sessions.json")
    devices = DeviceStore(settings.data_dir / "devices.json")
    queue = QueueStore(settings.data_dir / "queue.json")
    folders = FolderStore(settings.data_dir / "folders.json")
    pairing = Pairing()
    limiter = LoginLimiter()
    pair_start_limiter = LoginLimiter(PAIR_STARTS_PER_WINDOW, PAIR_CODE_TTL)
    hub = Hub()
    store = Store(settings.data_dir / "videos.json")
    # A wrong value in the environment must not stop the service either.
    defaults = Encoding(
        max_bitrate_kbps=settings.max_bitrate_kbps, x264_preset=settings.x264_preset
    )
    if defaults.problem():
        log.warning("Ignoring PROMIN_MAX_BITRATE_KBPS and PROMIN_X264_PRESET: %s", defaults.problem())
        defaults = Encoding()
    encoding = EncodingStore(settings.data_dir / "encoding.json", defaults)
    transcoder = Transcoder(settings, store, encoding)
    tvtest = TvTest(settings.data_dir / "tvtest.json")
    speed_payload: List[bytes] = []  # random bytes for the speed measurement, made on first use

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        for folder in (settings.uploads_dir, settings.videos_dir, settings.thumbs_dir):
            folder.mkdir(parents=True, exist_ok=True)
        # Half-written files are useless after a restart.
        for leftover in list(settings.uploads_dir.glob("*.part")) + list(
            settings.videos_dir.glob("*.tmp.mp4")
        ):
            leftover.unlink(missing_ok=True)
        store.load()
        encoding.load()
        users.load()
        sessions.load()
        devices.load()
        queue.load()
        folders.load()
        tvtest.load()
        await transcoder.start()
        yield
        if tvtest.task:
            tvtest.task.cancel()
            with suppress(asyncio.CancelledError):
                await tvtest.task
        await transcoder.stop()

    app = FastAPI(title="Promin", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.store = store
    app.state.transcoder = transcoder
    app.state.encoding = encoding
    app.state.users = users
    app.state.sessions = sessions
    app.state.hub = hub
    app.state.devices = devices
    app.state.queue = queue
    app.state.folders = folders
    app.state.pairing = pairing
    app.state.tvtest = tvtest
    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

    def fingerprint(username: str) -> Optional[str]:
        """What a session depends on: a password change or deletion revokes it."""
        if username == ADMIN:
            source = "admin:" + settings.token
        else:
            user = users.get(username)
            if user is None:
                return None
            source = user["hash"]
        message = (username + "|" + source).encode("utf-8")
        return hmac.new(secret, message, "sha256").hexdigest()

    def current_user(request: HTTPConnection) -> Optional[Dict[str, Any]]:
        raw = request.cookies.get(COOKIE_NAME, "")
        if raw.startswith("@"):  # a paired projector
            device_id, _, secret = raw[1:].partition(".")
            device = devices.get(device_id)
            digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()
            if device is None or not same(digest, device["hash"]):
                return None
            idle = time.time() - devices.seen(device)
            if idle > DEVICE_IDLE_TTL:
                devices.remove(device_id)
                return None
            if idle > SESSION_TOUCH_EVERY:
                devices.touch(device_id)
            return {"username": device["name"], "role": "device", "device_id": device_id}
        session_id, _, secret = raw.partition(".")
        session = sessions.get(session_id) if session_id and secret else None
        digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()
        if session is None or not same(digest, session["hash"]):
            return None
        username = session["username"]
        current = fingerprint(username)
        idle = time.time() - session["seen"]
        if current is None or not same(current, session["fingerprint"]) or idle > SESSION_IDLE_TTL:
            sessions.remove(session_id)
            return None
        if idle > SESSION_TOUCH_EVERY:
            sessions.touch(session_id)
        role = "admin" if username == ADMIN else (users.get(username) or {}).get("role")
        return {"username": username, "role": role}

    def require_auth(request: Request) -> Dict[str, Any]:
        user = current_user(request)
        if user is None:
            raise NotAuthenticated()
        return user

    def require_member(request: Request) -> Dict[str, Any]:
        """A person with a login, as opposed to a paired projector."""
        user = require_auth(request)
        if user["role"] == "device":
            raise HTTPException(403, "Проектор може лише показувати відео")
        return user

    def require_admin(request: Request) -> Dict[str, Any]:
        user = require_auth(request)
        if user["role"] != "admin":
            raise HTTPException(403, "Це може робити лише адміністратор")
        return user

    async def check_password(username: str, password: str) -> bool:
        if username == ADMIN:
            return same(password, settings.token)
        user = users.get(username)
        # Hash for an unknown name too, so the response does not reveal whether it exists.
        salt = bytes.fromhex(user["salt"]) if user else b"\0" * 16
        digest = await asyncio.to_thread(hash_password, password, salt)
        return user is not None and same(digest, user["hash"])

    async def drop_lost_connections() -> None:
        """Closes live connections whose login stopped working: a deleted user, a changed
        password, a logout or a disconnected projector. Otherwise an open remote would keep
        working until the phone reconnects."""
        for ws in list(hub.remotes) + ([hub.tv] if hub.tv is not None else []):
            if current_user(ws) is None:
                with suppress(Exception):
                    await ws.close(code=WS_UNAUTHORIZED)

    @app.exception_handler(NotAuthenticated)
    async def not_authenticated(request: Request, exc: NotAuthenticated) -> Response:
        if request.method == "GET" and not request.url.path.startswith("/api/"):
            path = request.url.path
            if path == "/pair":  # a scanned QR code: come back to it after the login
                path += "?t=" + request.query_params.get("t", "")
            back = safe_next(path)
            target = "/login?next=" + quote(back, safe="") if back != "/" else "/login"
            return RedirectResponse(target, status_code=303)
        return JSONResponse({"detail": "Потрібен вхід"}, status_code=401)

    def safe_next(value: Optional[str]) -> str:
        if value in LOGIN_NEXT or (value and PAIR_NEXT_RE.fullmatch(value)):
            return value
        return "/"

    def set_login_cookie(request: Request, response: Response, value: str) -> None:
        # Behind an HTTPS proxy (see FORWARDED_ALLOW_IPS in .env.example) the cookie is marked
        # Secure, so the browser never sends it over plain HTTP.
        response.set_cookie(
            COOKIE_NAME, value, max_age=COOKIE_MAX_AGE, httponly=True, samesite="lax",
            secure=request.url.scheme == "https",
        )

    def login_page(error: str = "", status_code: int = 200, next_path: str = "/") -> HTMLResponse:
        page = (BASE_DIR / "templates" / "login.html").read_text(encoding="utf-8")
        page = page.replace("{{error}}", html.escape(error)).replace("{{next}}", html.escape(next_path))
        return HTMLResponse(page, status_code)

    @app.get("/login")
    async def login_form(request: Request) -> Response:
        next_path = safe_next(request.query_params.get("next"))
        if current_user(request):
            return RedirectResponse(next_path, status_code=303)
        return login_page(next_path=next_path)

    @app.post("/login")
    async def login(request: Request) -> Response:
        ip = request.client.host if request.client else "unknown"
        if limiter.blocked(ip):
            return login_page("Забагато спроб. Спробуйте знову за 15 хвилин.", 429)
        # Count the attempt before checking the password: there is no await between the limit
        # check and the record, so parallel requests cannot bypass the limit.
        attempt = limiter.fail(ip)
        # Read the body as a stream and stop at the limit: Content-Length may be missing.
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > LOGIN_MAX_BODY:
                return login_page("Забагато даних.", 413)
        form = parse_qs(body.decode("utf-8", errors="replace"))
        username = (form.get("username") or [""])[0].strip().lower()
        password = (form.get("password") or [""])[0]
        next_path = safe_next((form.get("next") or [""])[0])
        if not await check_password(username, password):
            return login_page("Неправильний логін або пароль.", 401, next_path)
        # A successful login forgives only its own attempt: earlier failures stay.
        limiter.forgive(ip, attempt)
        cookie = sessions.add(username, fingerprint(username) or "")
        response = RedirectResponse(next_path, status_code=303)
        set_login_cookie(request, response, cookie)
        return response

    # The projector page is open to everyone: without a login it only shows a QR code to pair.
    @app.get("/tv", response_class=HTMLResponse)
    async def tv_page() -> str:
        return (BASE_DIR / "templates" / "tv.html").read_text(encoding="utf-8")

    @app.post("/api/pair/start")
    async def pair_start(request: Request) -> Dict[str, Any]:
        ip = request.client.host if request.client else "unknown"
        if pair_start_limiter.blocked(ip):
            raise HTTPException(429, "Забагато спроб. Спробуйте пізніше.")
        pair_start_limiter.fail(ip)
        started = pairing.start()
        if started is None:
            raise HTTPException(429, "Забагато пристроїв чекає на підключення.")
        # The phone opens this address from its camera, so it is the one the projector used.
        url = "%spair?t=%s" % (request.base_url, started.pop("key"))
        picture = io.BytesIO()
        segno.make(url, error="m").save(
            picture, kind="svg", xmldecl=False, svgns=True, omitsize=True,
            scale=10, border=4, dark="#000", light="#fff",
        )
        return dict(started, url=url, qr=picture.getvalue().decode("utf-8"))

    @app.post("/api/pair/status")
    async def pair_status(body: PairToken, request: Request) -> Response:
        pairing.purge()
        entry = pairing.pending.get(body.token)
        if entry is None:
            return JSONResponse({"status": "expired"})
        if entry["approved_by"] is None:
            return JSONResponse({"status": "pending"})
        # Approved on the phone: the QR code is spent. The device is recorded only now, as
        # the projector collects its cookie, so an approval nobody collects leaves no device.
        del pairing.pending[body.token]
        response = JSONResponse({"status": "approved"})
        set_login_cookie(request, response, devices.add("Проектор", entry["approved_by"]))
        return response

    @app.post("/logout")
    async def logout(request: Request) -> Response:
        # Drop the session on the server too: a stolen cookie stops working after logout.
        raw = request.cookies.get(COOKIE_NAME, "")
        if raw and not raw.startswith("@"):
            session_id, _, secret = raw.partition(".")
            session = sessions.get(session_id)
            digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()
            if session is not None and same(digest, session["hash"]):
                sessions.remove(session_id)
                await drop_lost_connections()
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE_NAME)
        return response

    # router: everyone who is logged in, projectors included (pages, the list, video files);
    # member: people with a login (adding and deleting videos, encoding settings);
    # admin: administrators only (users and paired projectors).
    router = APIRouter(dependencies=[Depends(require_auth)])
    member = APIRouter(dependencies=[Depends(require_member)])
    admin = APIRouter(dependencies=[Depends(require_admin)])

    @router.get("/api/me")
    async def me(request: Request, response: Response) -> Dict[str, Any]:
        # Every page asks this as it opens, so the cookie is renewed here: the browser keeps
        # it for COOKIE_MAX_AGE after the last visit, not after the login or the pairing.
        set_login_cookie(request, response, request.cookies[COOKIE_NAME])
        return require_auth(request)

    @router.post("/api/pair/approve")
    async def pair_approve(body: PairKey, request: Request) -> Dict[str, Any]:
        user = require_auth(request)
        if user["role"] == "device":
            raise HTTPException(403, "Підключати проектор може лише користувач")
        entry = pairing.by_key(body.key)
        if entry is None:
            raise HTTPException(404, "Цей QR-код уже не діє. Зчитайте новий з екрана проектора.")
        entry["approved_by"] = user["username"]
        return {"ok": True}

    @router.get("/pair", response_class=HTMLResponse)
    async def pair_page(request: Request) -> Response:
        """Where the QR code leads: the phone asks before it lets the projector in."""
        if require_auth(request)["role"] == "device":
            return RedirectResponse("/", status_code=303)
        return HTMLResponse((BASE_DIR / "templates" / "pair.html").read_text(encoding="utf-8"))

    @admin.get("/api/devices")
    async def list_devices() -> Dict[str, Any]:
        return {"devices": devices.list()}

    @admin.delete("/api/devices/{device_id}", status_code=204)
    async def delete_device(device_id: str) -> Response:
        if not devices.remove(device_id):
            raise HTTPException(404, "Пристрій не знайдено")
        # If this projector is connected right now, playback on it stops at once.
        await drop_lost_connections()
        return Response(status_code=204)

    @router.get("/users", response_class=HTMLResponse)
    async def users_page(request: Request) -> Response:
        if require_auth(request)["role"] != "admin":
            return RedirectResponse("/", status_code=303)
        return HTMLResponse((BASE_DIR / "templates" / "users.html").read_text(encoding="utf-8"))

    @router.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request) -> Response:
        if require_auth(request)["role"] == "device":
            return RedirectResponse("/", status_code=303)
        return HTMLResponse((BASE_DIR / "templates" / "settings.html").read_text(encoding="utf-8"))

    def encoding_payload() -> Dict[str, Any]:
        return {
            "encoding": asdict(encoding.get()),
            "defaults": asdict(encoding.defaults),
            "choices": {
                "max_height": sorted(HEIGHTS),
                "max_fps": list(FPS_CHOICES),
                "max_bitrate_kbps": list(BITRATE_RANGE_KBPS),
                "crf": list(CRF_RANGE),
                "audio_bitrate_kbps": list(AUDIO_BITRATES_KBPS),
                "x264_preset": list(X264_PRESETS),
            },
            "suggested_kbps": SUGGESTED_KBPS,
        }

    @member.get("/api/settings")
    async def get_settings() -> Dict[str, Any]:
        return encoding_payload()

    @member.put("/api/settings")
    async def put_settings(body: EncodingBody) -> Dict[str, Any]:
        chosen = Encoding(**body.model_dump())
        problem = chosen.problem()
        if problem:
            raise HTTPException(400, problem)
        encoding.set(chosen)
        return encoding_payload()

    # --------------------------------------------------------- projector check

    async def run_tvtest(run: str) -> None:
        """Generates the clips that are missing, then hands the check to the projector."""
        try:
            settings.tvtest_dir.mkdir(parents=True, exist_ok=True)
            for level in tvtest.levels:
                await make_tvtest_clip(transcoder.ffmpeg or "", level, settings.tvtest_dir)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Could not make the clips for the projector check")
            if tvtest.run == run:
                tvtest.fail("Не вдалося підготувати тестові ролики на сервері")
            return
        if tvtest.run != run:
            return
        if hub.tv is None:
            tvtest.fail("Проектор від'єднався, поки готувалися тестові ролики")
            return
        tvtest.status, tvtest.seen = "running", time.time()
        await hub.send(hub.tv, {
            "type": "command", "action": "test", "run": run,
            "clips": [
                dict(level, url="/tvtest/clip/" + level["id"]) for level in tvtest.levels
            ],
            "seconds": TVTEST_MEASURE_SECONDS,
            "speed": {"url": "/tvtest/speed", "seconds": TVTEST_SPEED_SECONDS},
            "limits": {"min_advance": TVTEST_MIN_ADVANCE, "max_dropped": TVTEST_MAX_DROPPED},
        })

    def tvtest_message(message: Dict[str, Any]) -> None:
        """A report from the projector about the check that is running."""
        if tvtest.status != "running" or message.get("run") != tvtest.run:
            return
        tvtest.seen = time.time()
        if message.get("done"):
            tvtest.finish(message)
        elif message.get("aborted"):
            tvtest.fail("Перевірку перервано: проектор зайнятий показом відео")
        else:
            tvtest.step = max(0, min(int(finite(message.get("step"))), len(tvtest.levels)))

    @member.get("/api/tvtest")
    async def get_tvtest() -> Dict[str, Any]:
        return tvtest.view()

    @member.post("/api/tvtest", status_code=202)
    async def start_tvtest() -> Dict[str, Any]:
        if tvtest.view()["status"] != "idle":
            raise HTTPException(409, "Перевірка вже триває")
        if not transcoder.ffmpeg:
            raise HTTPException(503, "На сервері не встановлено ffmpeg")
        if hub.tv is None:
            raise HTTPException(409, "Проектор не на зв'язку: відкрийте на ньому сторінку /tv")
        if hub.state is None or not hub.state["started"]:
            raise HTTPException(409, "Спершу натисніть «Почати» на проекторі")
        if hub.state["video_id"]:
            raise HTTPException(409, "Спершу зупиніть відео на проекторі")
        tvtest.task = asyncio.create_task(run_tvtest(tvtest.begin()))
        return tvtest.view()

    @router.get("/tvtest/clip/{level_id}")
    async def tvtest_clip(level_id: str) -> FileResponse:
        # The path is built from a known level, so the id never reaches the disk raw.
        level = next((lv for lv in tvtest.levels if lv["id"] == level_id), None)
        path = settings.tvtest_dir / tvtest_clip_name(level) if level else None
        if path is None or not path.is_file():
            raise HTTPException(404, "Ролик не знайдено")
        return FileResponse(path, media_type="video/mp4", headers={"Cache-Control": "no-store"})

    @router.get("/tvtest/speed")
    async def tvtest_speed() -> Response:
        """Bytes that cannot be compressed: the projector times how fast they arrive."""
        if not speed_payload:
            speed_payload.append(os.urandom(TVTEST_SPEED_CHUNK))
        return Response(
            speed_payload[0], media_type="application/octet-stream",
            headers={"Cache-Control": "no-store"},
        )

    @admin.get("/api/users")
    async def list_users() -> Dict[str, Any]:
        builtin = {"username": ADMIN, "role": "admin", "created": None, "builtin": True}
        return {"users": [builtin] + users.list()}

    def check_new_password(password: str) -> None:
        if len(password) < MIN_PASSWORD_LENGTH:
            raise HTTPException(400, "Пароль має містити щонайменше %d символів" % MIN_PASSWORD_LENGTH)
        if len(password) > 200:
            raise HTTPException(400, "Пароль задовгий")

    @admin.post("/api/users", status_code=201)
    async def create_user(body: NewUser) -> Dict[str, Any]:
        username = body.username.strip().lower()
        if not USERNAME_RE.match(username):
            raise HTTPException(
                400, "Логін: від 2 до 32 символів, лише малі латинські літери, цифри, «-» та «_»"
            )
        if body.role not in ROLES:
            raise HTTPException(400, "Невідома роль")
        check_new_password(body.password)
        if username == ADMIN or not await asyncio.to_thread(
            users.add, username, body.password, body.role
        ):
            raise HTTPException(409, "Такий користувач уже є")
        return {"username": username, "role": body.role}

    @admin.post("/api/users/{username}/password", status_code=204)
    async def change_password(username: str, body: NewPassword) -> Response:
        if username == ADMIN:
            raise HTTPException(400, "Пароль вбудованого адміністратора задається в PROMIN_TOKEN")
        check_new_password(body.password)
        if not await asyncio.to_thread(users.set_password, username, body.password):
            raise HTTPException(404, "Користувача не знайдено")
        await drop_lost_connections()
        return Response(status_code=204)

    @admin.delete("/api/users/{username}", status_code=204)
    async def delete_user(username: str, request: Request) -> Response:
        if username == ADMIN:
            raise HTTPException(400, "Вбудованого адміністратора видалити не можна")
        if username == require_auth(request)["username"]:
            raise HTTPException(400, "Не можна видалити себе")
        if not users.remove(username):
            raise HTTPException(404, "Користувача не знайдено")
        await drop_lost_connections()
        return Response(status_code=204)

    def public(item: Dict[str, Any]) -> Dict[str, Any]:
        data = {key: item.get(key) for key in PUBLIC_KEYS}
        # A folder that is gone (deleted, or its file damaged) reads as no folder.
        if not folders.exists(data["folder"]):
            data["folder"] = None
        if item["status"] == "processing":
            data["progress"] = transcoder.progress.get(item["id"], 0.0)
        return data

    @router.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return (BASE_DIR / "templates" / "index.html").read_text(encoding="utf-8")

    @router.get("/api/videos")
    async def list_videos() -> Dict[str, Any]:
        usage = shutil.disk_usage(settings.data_dir)
        videos = [public(item) for item in store.list()]
        counts: Dict[Optional[str], int] = {}
        for video in videos:
            counts[video["folder"]] = counts.get(video["folder"], 0) + 1
        return {
            "videos": videos,
            "folders": [dict(f, count=counts.get(f["id"], 0)) for f in folders.list()],
            "disk": {"free": usage.free, "total": usage.total},
            "max_upload": settings.max_upload_bytes,
        }

    @member.post("/api/videos", status_code=201)
    async def upload_video(request: Request) -> Dict[str, Any]:
        name = clean_name(request.headers.get("x-filename", ""))
        # The folder the phone is looking at; one deleted meanwhile means no folder.
        folder = request.headers.get("x-folder") or None
        if not folders.exists(folder):
            folder = None
        ext = Path(name).suffix.lower()
        if ext not in ALLOWED_EXT:
            raise HTTPException(415, "Підтримуються лише відео MP4, MOV, M4V, MKV, WebM і 3GP")

        try:
            length = int(request.headers["content-length"])
        except (KeyError, ValueError):
            raise HTTPException(411, "Потрібен заголовок Content-Length")
        if length <= 0:
            raise HTTPException(400, "Порожній файл")
        if length > settings.max_upload_bytes:
            raise HTTPException(413, "Файл більший за дозволений розмір")

        # Space is needed for both the original and the transcoded file.
        free = shutil.disk_usage(settings.data_dir).free
        if free < length * 2 + settings.disk_reserve_bytes:
            raise HTTPException(507, "На сервері недостатньо вільного місця")

        video_id = uuid.uuid4().hex
        part = settings.uploads_dir / (video_id + ".part")
        received = 0
        try:
            async with await anyio.open_file(part, "wb") as f:
                async for chunk in request.stream():
                    received += len(chunk)
                    if received > length:
                        raise HTTPException(413, "Файл більший за заявлений розмір")
                    await f.write(chunk)
            if received != length:
                raise HTTPException(400, "Завантаження обірвалося")
        except ClientDisconnect:
            part.unlink(missing_ok=True)
            raise HTTPException(400, "Завантаження обірвалося")
        except BaseException:
            part.unlink(missing_ok=True)
            raise

        source = video_id + ext
        os.replace(part, settings.uploads_dir / source)
        item = {
            "id": video_id,
            "name": name,
            "size": length,
            "status": "queued",
            "created": time.time(),
            "source": source,
            "duration": None,
            "thumb": False,
            "error": None,
            "folder": folder,
        }
        store.add(item)
        transcoder.enqueue(video_id)
        return public(item)

    @member.post("/api/videos/{video_id}/retry")
    async def retry_video(video_id: str) -> Dict[str, Any]:
        item = store.get(video_id)
        if not item:
            raise HTTPException(404, "Відео не знайдено")
        if item["status"] != "error":
            raise HTTPException(409, "Повторити можна лише відео з помилкою")
        if not item.get("source") or not (settings.uploads_dir / item["source"]).is_file():
            raise HTTPException(409, "Оригінал не зберігся, завантажте відео ще раз")
        store.update(video_id, status="queued", error=None)
        transcoder.enqueue(video_id)
        return public(store.get(video_id) or item)

    @router.get("/media/{video_id}")
    async def media(video_id: str) -> FileResponse:
        item = store.get(video_id)
        path = settings.videos_dir / (video_id + ".mp4")
        if not item or item["status"] != "ready" or not path.is_file():
            raise HTTPException(404, "Відео не знайдено")
        # FileResponse handles Range requests itself; seeking needs them.
        return FileResponse(path, media_type="video/mp4")

    @router.get("/thumb/{video_id}")
    async def thumb(video_id: str) -> FileResponse:
        item = store.get(video_id)
        path = settings.thumbs_dir / (video_id + ".jpg")
        if not item or not path.is_file():
            raise HTTPException(404, "Мініатюру не знайдено")
        return FileResponse(
            path, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"}
        )

    def delete_many(video_ids: List[str]) -> List[str]:
        """Deletes videos and returns the ids that existed. The list is written once, however
        many videos go: every write waits for the disk and holds up everything else."""
        # Files are touched only for ids the store knew, so an id never reaches the glob raw.
        deleted = store.remove_many(video_ids)
        for video_id in deleted:
            transcoder.cancel(video_id)
            remove_files(settings, video_id)
        return deleted

    @member.delete("/api/videos/{video_id}", status_code=204)
    async def delete_video(video_id: str) -> Response:
        if not delete_many([video_id]):
            raise HTTPException(404, "Відео не знайдено")
        if queue.remove_videos({video_id}):
            await queue_changed()
        return Response(status_code=204)

    @admin.post("/api/videos/delete")
    async def delete_videos(body: VideoIds) -> Dict[str, Any]:
        """Deletes several videos at once. Ids that are already gone are reported, not an error."""
        ids = list(dict.fromkeys(body.ids))  # without repeats, in the given order
        if not ids:
            raise HTTPException(400, "Не вибрано жодного відео")
        if len(ids) > BATCH_DELETE_MAX:
            raise HTTPException(400, "За раз можна видалити щонайбільше %d відео" % BATCH_DELETE_MAX)
        deleted = delete_many(ids)
        gone = set(deleted)
        if queue.remove_videos(gone):
            await queue_changed()
        return {"deleted": deleted, "missing": [i for i in ids if i not in gone]}

    # ----------------------------------------------------------------- folders

    def folder_name(raw: str) -> str:
        try:
            return clean_folder_name(raw)
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @member.post("/api/folders", status_code=201)
    async def create_folder(body: FolderName) -> Dict[str, Any]:
        name = folder_name(body.name)
        try:
            folder_id = folders.add(name)
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        return {"id": folder_id, "name": name}

    @member.patch("/api/folders/{folder_id}")
    async def rename_folder(folder_id: str, body: FolderName) -> Dict[str, Any]:
        name = folder_name(body.name)
        try:
            if not folders.rename(folder_id, name):
                raise HTTPException(404, "Теку не знайдено")
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        return {"id": folder_id, "name": name}

    @member.delete("/api/folders/{folder_id}", status_code=204)
    async def delete_folder(folder_id: str) -> Response:
        """Deletes a folder; its videos stay in the library without a folder."""
        if not folders.remove(folder_id):
            raise HTTPException(404, "Теку не знайдено")
        store.clear_folder(folder_id)
        return Response(status_code=204)

    @member.post("/api/videos/{video_id}/folder")
    async def move_video(video_id: str, body: VideoFolder) -> Dict[str, Any]:
        if body.folder is not None and not folders.exists(body.folder):
            raise HTTPException(404, "Теку не знайдено")
        if not store.update(video_id, folder=body.folder):
            raise HTTPException(404, "Відео не знайдено")
        item = store.get(video_id)
        if item is None:  # deleted a moment ago
            raise HTTPException(404, "Відео не знайдено")
        return public(item)

    # ------------------------------------------------------------- watch queue

    def queue_view() -> List[Dict[str, Any]]:
        """The queue as phones show it: only videos that can still be played."""
        items = []
        for entry in queue.list():
            item = store.get(entry["video_id"])
            if item and item["status"] == "ready":
                items.append({
                    "entry": entry["entry"], "video_id": item["id"], "name": item["name"],
                    "duration": item.get("duration"), "thumb": bool(item.get("thumb")),
                    "thumb_v": item.get("thumb_v"),
                })
        return items

    async def queue_changed() -> Dict[str, Any]:
        view = queue_view()
        await hub.broadcast({"type": "queue", "queue": view})
        return {"queue": view}

    async def play_next() -> bool:
        """Loads the first playable queue entry on the projector; False if there is none."""
        if hub.tv is None:
            return False
        popped = played = False
        while not played:
            entry = queue.pop()
            if entry is None:
                break
            popped = True
            item = store.get(entry["video_id"])
            if item and item["status"] == "ready":  # skip videos deleted since they were queued
                await hub.send(hub.tv, {
                    "type": "command", "action": "load", "video_id": item["id"], "name": item["name"],
                })
                played = True
        if popped:
            await queue_changed()
        return played

    @member.get("/api/queue")
    async def get_queue() -> Dict[str, Any]:
        return {"queue": queue_view()}

    @member.post("/api/queue", status_code=201)
    async def add_to_queue(body: QueueAdd) -> Dict[str, Any]:
        item = store.get(body.video_id)
        if not item or item["status"] != "ready":
            raise HTTPException(404, "Це відео недоступне")
        if queue.add(item["id"]) is None:
            raise HTTPException(409, "У черзі вже %d відео" % QUEUE_MAX)
        return await queue_changed()

    @member.delete("/api/queue/{entry}")
    async def remove_from_queue(entry: str) -> Dict[str, Any]:
        if not queue.remove(entry):
            raise HTTPException(404, "Цього відео вже немає в черзі")
        return await queue_changed()

    @member.post("/api/queue/{entry}/move")
    async def move_in_queue(entry: str, body: QueueMove) -> Dict[str, Any]:
        if not queue.move(entry, body.position):
            raise HTTPException(404, "Цього відео вже немає в черзі")
        return await queue_changed()

    @member.delete("/api/queue")
    async def clear_queue() -> Dict[str, Any]:
        queue.clear()
        return await queue_changed()

    @app.websocket("/ws")
    async def websocket(ws: WebSocket) -> None:
        role = ws.query_params.get("role")
        # A browser always names the page that opens the connection. A foreign one (including
        # another port of this server, which receives the cookie too) is refused.
        origin = ws.headers.get("origin")
        if origin is not None and urlsplit(origin).netloc != ws.headers.get("host"):
            await ws.close(code=1008)
            return
        await ws.accept()
        user = current_user(ws)
        if user is None:
            await ws.close(code=WS_UNAUTHORIZED)
            return
        ws.state.device_id = user.get("device_id")
        if role == "tv":
            await serve_tv(ws)
        elif role == "remote" and user["role"] != "device":  # a projector only shows
            await serve_remote(ws)
        else:
            await ws.close(code=1008)

    async def serve_tv(ws: WebSocket) -> None:
        # There is one projector: a new connection takes over and the previous one is closed.
        previous, hub.tv, hub.state = hub.tv, ws, None
        if tvtest.status == "running":  # the page that was running the check is gone
            tvtest.fail("Сторінку проектора відкрили заново під час перевірки")
        if previous is not None:
            with suppress(Exception):
                await previous.close(code=WS_REPLACED)
        await hub.broadcast({"type": "tv", "connected": True})
        try:
            while True:
                message = await ws.receive_json()
                if not isinstance(message, dict):
                    continue
                if message.get("type") == "state":
                    hub.state = clean_state(message)
                    await hub.broadcast({"type": "state", **hub.state})
                elif message.get("type") == "ended":
                    # Only the video that is playing can end: a late or repeated report
                    # must not skip the next one in the queue.
                    playing = hub.state.get("video_id") if hub.state else None
                    if playing and message.get("video_id") == playing:
                        await play_next()
                elif message.get("type") == "test":
                    tvtest_message(message)
        except WebSocketDisconnect:
            pass
        except Exception:  # the page sent something that is not JSON, or handling it failed
            log.exception("The projector connection failed")
            with suppress(Exception):
                await ws.close(code=1003)
        finally:
            if hub.tv is ws:
                hub.tv, hub.state = None, None
                if tvtest.status == "running":
                    tvtest.fail("Проектор від'єднався під час перевірки")
                await hub.broadcast({"type": "tv", "connected": False})

    async def serve_remote(ws: WebSocket) -> None:
        hub.remotes.add(ws)
        await hub.send(ws, {"type": "tv", "connected": hub.tv is not None})
        if hub.state is not None:
            await hub.send(ws, {"type": "state", **hub.state})
        # An empty queue is not sent: the page clears its copy whenever the connection drops.
        view = queue_view()
        if view:
            await hub.send(ws, {"type": "queue", "queue": view})
        try:
            while True:
                message = await ws.receive_json()
                if not isinstance(message, dict) or message.get("type") != "command":
                    continue
                action = message.get("action")
                if action not in REMOTE_ACTIONS:
                    continue
                if hub.tv is None:
                    await hub.send(ws, {"type": "error", "message": "Проектор не на зв'язку"})
                    continue
                if action == "next":
                    if not await play_next():
                        await hub.send(ws, {"type": "error", "message": "Черга порожня"})
                    continue
                command: Dict[str, Any] = {"type": "command", "action": action}
                if action == "load":
                    item = store.get(str(message.get("video_id")))
                    if not item or item["status"] != "ready":
                        await hub.send(ws, {"type": "error", "message": "Це відео недоступне"})
                        continue
                    command.update(video_id=item["id"], name=item["name"])
                elif action in ("seek", "seek_by", "volume"):
                    try:
                        command["value"] = float(message.get("value"))
                    except (TypeError, ValueError):
                        continue
                await hub.send(hub.tv, command)
        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("A remote connection failed")
            with suppress(Exception):
                await ws.close(code=1003)
        finally:
            hub.remotes.discard(ws)

    app.include_router(router)
    app.include_router(member)
    app.include_router(admin)
    return app


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    app = create_app()
    # One process: the queue and the remote state live in memory.
    uvicorn.run(app, host="0.0.0.0", port=app.state.settings.port)
