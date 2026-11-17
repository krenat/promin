"""Promin — домашній відеосервер для проектора.

Список відео, завантаження, перекодування, перегляд, видалення, користувачі з ролями
і пульт: телефон керує сторінкою проектора через WebSocket. Проектор підключається
кодом з екрана, який підтверджують на телефоні.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote

import anyio
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
PUBLIC_KEYS = ("id", "name", "size", "status", "created", "duration", "thumb", "thumb_v", "error")

# Версія способу, яким зроблено мініатюру: після її зміни старі мініатюри перероблюються.
THUMB_VERSION = 2

COOKIE_NAME = "promin_session"
COOKIE_MAX_AGE = 365 * 24 * 3600
MIN_TOKEN_LENGTH = 12

# Куди можна повернути після входу і які команди пульт може слати проектору.
LOGIN_NEXT = ("/", "/tv")
REMOTE_ACTIONS = ("load", "play", "pause", "seek", "seek_by", "volume", "stop")
WS_REPLACED = 4000  # сторінку проектора відкрито деінде
WS_UNAUTHORIZED = 4401

# Підключення проектора кодом: скільки живе код і скільки їх може чекати водночас.
PAIR_CODE_TTL = 10 * 60
PAIR_MAX_PENDING = 50
PAIR_STARTS_PER_WINDOW = 10

# Вбудований адміністратор: його пароль — це PROMIN_TOKEN, у файлі він не зберігається.
ADMIN = "admin"
ROLES = ("admin", "viewer")
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")
MIN_PASSWORD_LENGTH = 8
PBKDF2_ROUNDS = 200_000
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SECONDS = 15 * 60

# Межі, у які має вписатися готове відео.
MAX_WIDTH = 1920
MAX_HEIGHT = 1080
MAX_FPS = 60
HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}  # PQ (HDR10) і HLG

# Ланцюжки HDR → SDR. Перший потребує ffmpeg з libzimg, другий — нового swscale.
HDR_FILTER_ZSCALE = (
    "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
    "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv"
)
HDR_FILTER_SWSCALE = (
    "scale=out_transfer=linear:out_primaries=bt709,format=gbrpf32le,"
    "tonemap=tonemap=hable:desat=0,"
    "scale=out_transfer=bt709:out_color_matrix=bt709:out_range=tv"
)


@dataclass
class Settings:
    data_dir: Path
    token: str = ""
    port: int = 8080
    max_upload_bytes: int = 4096 * MB
    # Скільки вільного місця має лишитися на диску після завантаження.
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


class Store:
    """Список відео у JSON-файлі. Запис під блокуванням і атомарний."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._items: Dict[str, Dict[str, Any]] = {}

    def load(self) -> None:
        with self._lock:
            if self.path.exists():
                self._items = json.loads(self.path.read_text(encoding="utf-8"))
            else:
                self._items = {}

    def _save(self) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(
            json.dumps(self._items, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, self.path)

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
        with self._lock:
            if video_id not in self._items:
                return False
            del self._items[video_id]
            self._save()
            return True


# ---------------------------------------------------------------- перекодування


class ProcessingError(Exception):
    """Помилка обробки, текст якої показуємо користувачу."""


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


def plan_encode(probe: Dict[str, Any], settings: Settings) -> Dict[str, Any]:
    """За виводом ffprobe вирішує, скільки роботи потрібно.

    remux — файл уже підходить, лише перепакувати;
    audio — відео підходить, перекодувати тільки звук;
    transcode — перекодувати все.
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

    # Відео не чіпаємо лише тоді, коли проектор точно зіграє його як є.
    video_ok = (
        video.get("codec_name") == "h264"
        and video.get("pix_fmt") == "yuv420p"
        and not hdr
        and stream_rotation(video) == 0
        and 0 < int(video.get("width") or 0) <= MAX_WIDTH
        and 0 < int(video.get("height") or 0) <= MAX_HEIGHT
        and 0 < fps <= MAX_FPS + 0.5
        and 0 < bitrate <= settings.max_bitrate_kbps * 1000 * 1.25
    )
    # Звук лишаємо як є, якщо це AAC моно або стерео.
    audio_ok = audio is None or (
        audio.get("codec_name") == "aac" and int(audio.get("channels") or 2) <= 2
    )
    if video_ok and audio_ok:
        mode = "remux"
    elif video_ok:
        mode = "audio"
    else:
        mode = "transcode"
    return {
        "mode": mode,
        "duration": duration,
        "fps": fps,
        "hdr": hdr,
    }


def build_ffmpeg_args(
    ffmpeg: str,
    src: Path,
    dst: Path,
    plan: Dict[str, Any],
    settings: Settings,
    hdr_filter: Optional[str],
) -> List[str]:
    args = [ffmpeg, "-nostdin", "-y", "-v", "error", "-i", str(src)]
    args += ["-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn"]
    if plan["mode"] == "remux":
        args += ["-c", "copy"]
    elif plan["mode"] == "audio":
        # Картинка копіюється без втрат, перекодовується лише звук.
        args += ["-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-ac", "2"]
    else:
        tonemapped = bool(plan["hdr"] and hdr_filter)
        # Вписати в 1920×1080 без збільшення; поворот ffmpeg застосовує сам.
        filters = [
            "scale=w='min(%d,iw)':h='min(%d,ih)'"
            ":force_original_aspect_ratio=decrease:force_divisible_by=2"
            % (MAX_WIDTH, MAX_HEIGHT)
        ]
        # Tone mapping після зменшення: на 4K він утричі повільніший.
        if tonemapped:
            filters.append(hdr_filter)
        if plan["fps"] > MAX_FPS + 0.5:
            filters.append("fps=%d" % MAX_FPS)
        filters.append("format=yuv420p")
        rate = settings.max_bitrate_kbps
        args += [
            "-vf", ",".join(filters),
            "-c:v", "libx264",
            "-preset", settings.x264_preset,
            "-profile:v", "high",
            "-crf", "21",
            "-maxrate", "%dk" % rate,
            "-bufsize", "%dk" % (rate * 2),
            # Ключовий кадр кожні 2 секунди — для швидкого перемотування.
            "-force_key_frames", "expr:gte(t,n_forced*2)",
            "-c:a", "aac", "-b:a", "160k", "-ac", "2",
        ]
        if tonemapped:
            args += ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"]
    # Метадані прибираємо: у відео з телефона там буває геолокація.
    args += ["-map_metadata", "-1", "-movflags", "+faststart"]
    args += ["-progress", "pipe:1", "-nostats", str(dst)]
    return args


def detect_hdr_filter(ffmpeg: str) -> Optional[str]:
    """Який ланцюжок HDR → SDR уміє встановлений ffmpeg."""

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
    """Черга з одним воркером: відео обробляються по одному."""

    def __init__(self, settings: Settings, store: Store) -> None:
        self.settings = settings
        self.store = store
        self.ffmpeg = shutil.which("ffmpeg")
        self.ffprobe = shutil.which("ffprobe")
        self.hdr_filter: Optional[str] = None
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
                log.warning("ffmpeg не вміє tone mapping: HDR-відео можуть бути блідими")
        else:
            log.warning("ffmpeg не знайдено: відео не оброблятимуться")
        # Завдання, які не встигли завершитися до перезапуску, повертаємо в чергу.
        pending = [
            item for item in self.store.list() if item["status"] in ("queued", "processing")
        ]
        for item in sorted(pending, key=lambda item: item["created"]):
            self.store.update(item["id"], status="queued", error=None)
            self.enqueue(item["id"])
        self._task = asyncio.create_task(self._run())
        self._thumb_task = asyncio.create_task(self._refresh_thumbnails())

    async def stop(self) -> None:
        for task in (self._thumb_task, self._task):
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    async def _refresh_thumbnails(self) -> None:
        """Перероблює мініатюри, зроблені старим способом (часто це був чорний кадр)."""
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
                log.exception("Не вдалося переробити мініатюру %s", item["id"])
                continue
            self.store.update(
                item["id"], thumb=made or bool(item.get("thumb")), thumb_v=THUMB_VERSION
            )

    def enqueue(self, video_id: str) -> None:
        assert self._queue is not None
        self._queue.put_nowait(video_id)

    def cancel(self, video_id: str) -> None:
        """Зупиняє ffmpeg, якщо він зараз обробляє це відео."""
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
            except Exception as exc:  # воркер не має падати через одне відео
                log.exception("Не вдалося обробити %s", video_id)
                self.store.update(video_id, status="error", error=str(exc)[-2000:])

    async def _process(self, video_id: str) -> None:
        item = self.store.get(video_id)
        if not item or item["status"] != "queued":
            return
        settings = self.settings
        src = settings.uploads_dir / item["source"]
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

            plan = plan_encode(await self._probe(src), settings)
            args = build_ffmpeg_args(self.ffmpeg, src, tmp, plan, settings, self.hdr_filter)
            code, err = await self._ffmpeg(args, video_id, plan["duration"])

            if self.store.get(video_id) is None:  # видалили під час обробки
                remove_files(settings, video_id)
                return
            if code != 0:
                raise ProcessingError(err.strip() or "ffmpeg завершився з кодом %d" % code)

            os.replace(tmp, final)
            thumb = await self._thumbnail(final, video_id, plan["duration"])
            updated = self.store.update(
                video_id,
                status="ready",
                size=final.stat().st_size,
                duration=plan["duration"] or None,
                thumb=thumb,
                thumb_v=THUMB_VERSION,
                error=None,
            )
            if not updated:
                remove_files(settings, video_id)
                return
            src.unlink(missing_ok=True)  # оригінал лишається на телефоні
        except ProcessingError as exc:
            tmp.unlink(missing_ok=True)
            # Шляхи на сервері користувачу ні до чого.
            text = str(exc).replace(str(src), item["name"]).replace(str(tmp), "готовий файл")
            self.store.update(video_id, status="error", error=text[-2000:])
        finally:
            proc = self._current_proc
            if proc and proc.returncode is None:
                proc.kill()
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
            out, err = await proc.communicate()
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
        try:
            async for raw in proc.stdout:
                key, _, value = raw.decode(errors="replace").strip().partition("=")
                # out_time_ms у ffmpeg теж у мікросекундах — історична особливість.
                if key in ("out_time_us", "out_time_ms") and duration > 0:
                    with suppress(ValueError):
                        done = int(value) / 1_000_000 / duration
                        self.progress[video_id] = max(0.0, min(done, 1.0))
            code = await proc.wait()
            return code, (await err_task).decode(errors="replace")
        finally:
            err_task.cancel()
            await self._reap(proc)

    @staticmethod
    async def _reap(proc: asyncio.subprocess.Process) -> None:
        """Не лишає процес працювати, якщо обробку перервали."""
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
            await proc.wait()
        finally:
            await self._reap(proc)
        if proc.returncode != 0 or not tmp.is_file():
            tmp.unlink(missing_ok=True)
            return False
        os.replace(tmp, out)
        return True


def thumbnail_args(ffmpeg: str, video: Path, out: Path, duration: float) -> List[str]:
    """Кадр для мініатюри: не з самого початку, де часто чорний екран або заставка."""
    if duration > 20:
        # З десятої частини відео беремо найтиповіший кадр із двох секунд.
        seek, filters = duration * 0.1, "thumbnail=50,scale=640:-2"
    else:
        seek, filters = min(1.0, duration / 2), "scale=640:-2"
    return [
        ffmpeg, "-nostdin", "-y", "-v", "error",
        "-ss", "%.2f" % seek, "-i", str(video),
        "-frames:v", "1", "-vf", filters, "-q:v", "5", "-f", "image2", str(out),
    ]


# ------------------------------------------------------------------------- вхід


class NotAuthenticated(Exception):
    pass


class LoginLimiter:
    """Обмежує кількість невдалих спроб входу з однієї адреси."""

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

    def fail(self, ip: str) -> None:
        self._failures.setdefault(ip, []).append(time.monotonic())

    def reset(self, ip: str) -> None:
        self._failures.pop(ip, None)


def same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS).hex()


class UserStore:
    """Користувачі у JSON-файлі; паролі лише у вигляді хешів."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._users: Dict[str, Dict[str, Any]] = {}

    def load(self) -> None:
        with self._lock:
            if self.path.exists():
                self._users = json.loads(self.path.read_text(encoding="utf-8"))
            else:
                self._users = {}

    def _save(self) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self._users, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

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


class DeviceStore:
    """Підключені проектори. Зберігаємо лише хеш секрету з cookie пристрою."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._devices: Dict[str, Dict[str, Any]] = {}

    def load(self) -> None:
        with self._lock:
            if self.path.exists():
                self._devices = json.loads(self.path.read_text(encoding="utf-8"))
            else:
                self._devices = {}

    def _save(self) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self._devices, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def add(self, name: str, approved_by: str) -> str:
        """Створює пристрій і повертає значення його cookie."""
        device_id = secrets.token_hex(8)
        secret = secrets.token_urlsafe(32)
        with self._lock:
            self._devices[device_id] = {
                "name": name,
                "hash": hashlib.sha256(secret.encode("utf-8")).hexdigest(),
                "approved_by": approved_by,
                "created": time.time(),
            }
            self._save()
        return "@%s.%s" % (device_id, secret)

    def get(self, device_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            device = self._devices.get(device_id)
            return dict(device) if device else None

    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            items = [
                {"id": key, "name": d["name"], "approved_by": d["approved_by"], "created": d["created"]}
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
    """Коди, які зараз показують проектори. Живуть лише в пам'яті."""

    def __init__(self) -> None:
        self.pending: Dict[str, Dict[str, Any]] = {}  # токен опитування → запис

    def purge(self) -> None:
        cutoff = time.monotonic() - PAIR_CODE_TTL
        for token in [t for t, entry in self.pending.items() if entry["created"] < cutoff]:
            del self.pending[token]

    def start(self) -> Optional[Dict[str, Any]]:
        self.purge()
        if len(self.pending) >= PAIR_MAX_PENDING:
            return None
        taken = {entry["code"] for entry in self.pending.values()}
        code = "%06d" % secrets.randbelow(10**6)
        while code in taken:
            code = "%06d" % secrets.randbelow(10**6)
        token = secrets.token_urlsafe(24)
        self.pending[token] = {"code": code, "created": time.monotonic(), "cookie": None}
        return {"code": code, "token": token, "expires_in": PAIR_CODE_TTL}

    def by_code(self, code: str) -> Optional[Dict[str, Any]]:
        self.purge()
        for entry in self.pending.values():
            if entry["cookie"] is None and same(entry["code"], code):
                return entry
        return None


class PairToken(BaseModel):
    token: str


class PairCode(BaseModel):
    code: str


class NewUser(BaseModel):
    username: str
    password: str
    role: str = "viewer"


class NewPassword(BaseModel):
    password: str


# ------------------------------------------------------------------------ пульт


class Hub:
    """Зв'язує пульти з проектором. Стан живе в пам'яті одного процесу."""

    def __init__(self) -> None:
        self.tv: Optional[WebSocket] = None
        self.remotes: set = set()
        self.state: Optional[Dict[str, Any]] = None

    @staticmethod
    async def send(ws: WebSocket, message: Dict[str, Any]) -> None:
        with suppress(Exception):  # з'єднання могло щойно обірватися
            await ws.send_json(message)

    async def broadcast(self, message: Dict[str, Any]) -> None:
        for ws in list(self.remotes):
            await self.send(ws, message)


def clean_state(message: Dict[str, Any]) -> Dict[str, Any]:
    """Стан від сторінки проектора: лише відомі поля відомих типів."""

    def number(key: str) -> float:
        try:
            value = float(message.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0
        return value if value == value and abs(value) != float("inf") else 0.0

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


# ------------------------------------------------------------------- застосунок


def clean_name(raw: str) -> str:
    """Назва для показу: без шляху, обмеженої довжини."""
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
            "Задайте PROMIN_TOKEN: щонайменше %d символів" % MIN_TOKEN_LENGTH
        )
    # Ключ для підпису cookie; сам токен у cookie не потрапляє.
    secret = hmac.new(settings.token.encode("utf-8"), b"promin-session", "sha256").digest()
    users = UserStore(settings.data_dir / "users.json")
    devices = DeviceStore(settings.data_dir / "devices.json")
    pairing = Pairing()
    limiter = LoginLimiter()
    pair_start_limiter = LoginLimiter(PAIR_STARTS_PER_WINDOW, PAIR_CODE_TTL)
    pair_code_limiter = LoginLimiter(10, LOGIN_WINDOW_SECONDS)
    hub = Hub()
    store = Store(settings.data_dir / "videos.json")
    transcoder = Transcoder(settings, store)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        for folder in (settings.uploads_dir, settings.videos_dir, settings.thumbs_dir):
            folder.mkdir(parents=True, exist_ok=True)
        # Недописані файли після перезапуску нікому не потрібні.
        for leftover in list(settings.uploads_dir.glob("*.part")) + list(
            settings.videos_dir.glob("*.tmp.mp4")
        ):
            leftover.unlink(missing_ok=True)
        store.load()
        users.load()
        devices.load()
        await transcoder.start()
        yield
        await transcoder.stop()

    app = FastAPI(title="Promin", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.store = store
    app.state.transcoder = transcoder
    app.state.users = users
    app.state.hub = hub
    app.state.devices = devices
    app.state.pairing = pairing
    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

    def fingerprint(username: str) -> Optional[str]:
        """Те, від чого залежить сесія: зміна пароля чи видалення її скасовує."""
        if username == ADMIN:
            return "admin:" + settings.token
        user = users.get(username)
        return user["hash"] if user else None

    def session_cookie(username: str) -> str:
        message = (username + "|" + (fingerprint(username) or "")).encode("utf-8")
        return username + "." + hmac.new(secret, message, "sha256").hexdigest()

    def current_user(request: HTTPConnection) -> Optional[Dict[str, Any]]:
        raw = request.cookies.get(COOKIE_NAME, "")
        if raw.startswith("@"):  # підключений проектор
            device_id, _, secret = raw[1:].partition(".")
            device = devices.get(device_id)
            digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()
            if device is None or not same(digest, device["hash"]):
                return None
            return {"username": device["name"], "role": "device", "device_id": device_id}
        username = raw.partition(".")[0]
        if not username or fingerprint(username) is None:
            return None
        if not same(raw, session_cookie(username)):
            return None
        role = "admin" if username == ADMIN else (users.get(username) or {}).get("role")
        return {"username": username, "role": role}

    def require_auth(request: Request) -> Dict[str, Any]:
        user = current_user(request)
        if user is None:
            raise NotAuthenticated()
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
        # Для невідомого імені теж рахуємо хеш, щоб відповідь не видавала, чи воно існує.
        salt = bytes.fromhex(user["salt"]) if user else b"\0" * 16
        digest = await asyncio.to_thread(hash_password, password, salt)
        return user is not None and same(digest, user["hash"])

    @app.exception_handler(NotAuthenticated)
    async def not_authenticated(request: Request, exc: NotAuthenticated) -> Response:
        if request.method == "GET" and not request.url.path.startswith("/api/"):
            path = request.url.path
            target = "/login?next=" + path if path in LOGIN_NEXT and path != "/" else "/login"
            return RedirectResponse(target, status_code=303)
        return JSONResponse({"detail": "Потрібен вхід"}, status_code=401)

    def safe_next(value: Optional[str]) -> str:
        return value if value in LOGIN_NEXT else "/"

    def login_page(error: str = "", status_code: int = 200, next_path: str = "/") -> HTMLResponse:
        page = (BASE_DIR / "templates" / "login.html").read_text(encoding="utf-8")
        page = page.replace("{{error}}", html.escape(error)).replace("{{next}}", next_path)
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
        if int(request.headers.get("content-length") or 0) > 4096:
            return login_page("Забагато даних.", 413)
        form = parse_qs((await request.body()).decode("utf-8", errors="replace"))
        username = (form.get("username") or [""])[0].strip().lower()
        password = (form.get("password") or [""])[0]
        next_path = safe_next((form.get("next") or [""])[0])
        if not await check_password(username, password):
            limiter.fail(ip)
            return login_page("Неправильний логін або пароль.", 401, next_path)
        limiter.reset(ip)
        response = RedirectResponse(next_path, status_code=303)
        response.set_cookie(
            COOKIE_NAME, session_cookie(username),
            max_age=COOKIE_MAX_AGE, httponly=True, samesite="lax",
        )
        return response

    # Сторінка проектора відкрита всім: без входу вона лише показує код підключення.
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
        return started

    @app.post("/api/pair/status")
    async def pair_status(body: PairToken) -> Response:
        pairing.purge()
        entry = pairing.pending.get(body.token)
        if entry is None:
            return JSONResponse({"status": "expired"})
        if entry["cookie"] is None:
            return JSONResponse({"status": "pending"})
        # Код підтвердили на телефоні: видаємо проектору його cookie, код більше не діє.
        del pairing.pending[body.token]
        response = JSONResponse({"status": "approved"})
        response.set_cookie(
            COOKIE_NAME, entry["cookie"], max_age=COOKIE_MAX_AGE, httponly=True, samesite="lax"
        )
        return response

    @app.post("/logout")
    async def logout() -> Response:
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE_NAME)
        return response

    # router — усім, хто увійшов (включно з відеофайлами); admin — лише адміністраторам.
    router = APIRouter(dependencies=[Depends(require_auth)])
    admin = APIRouter(dependencies=[Depends(require_admin)])

    @router.get("/api/me")
    async def me(request: Request) -> Dict[str, Any]:
        return require_auth(request)

    @router.post("/api/pair/approve")
    async def pair_approve(body: PairCode, request: Request) -> Dict[str, Any]:
        user = require_auth(request)
        if user["role"] == "device":
            raise HTTPException(403, "Підключати проектор може лише користувач")
        if pair_code_limiter.blocked(user["username"]):
            raise HTTPException(429, "Забагато спроб. Спробуйте знову за 15 хвилин.")
        code = "".join(ch for ch in body.code if ch.isdigit())
        entry = pairing.by_code(code) if len(code) == 6 else None
        if entry is None:
            pair_code_limiter.fail(user["username"])
            raise HTTPException(404, "Код не знайдено або він уже не діє")
        entry["cookie"] = devices.add("Проектор", user["username"])
        return {"ok": True}

    @admin.get("/api/devices")
    async def list_devices() -> Dict[str, Any]:
        return {"devices": devices.list()}

    @admin.delete("/api/devices/{device_id}", status_code=204)
    async def delete_device(device_id: str) -> Response:
        if not devices.remove(device_id):
            raise HTTPException(404, "Пристрій не знайдено")
        # Якщо цей проектор зараз на зв'язку, показ на ньому одразу припиняється.
        tv = hub.tv
        if tv is not None and getattr(tv.state, "device_id", None) == device_id:
            with suppress(Exception):
                await tv.close(code=WS_UNAUTHORIZED)
        return Response(status_code=204)

    @router.get("/users", response_class=HTMLResponse)
    async def users_page(request: Request) -> Response:
        if require_auth(request)["role"] != "admin":
            return RedirectResponse("/", status_code=303)
        return HTMLResponse((BASE_DIR / "templates" / "users.html").read_text(encoding="utf-8"))

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
        return Response(status_code=204)

    @admin.delete("/api/users/{username}", status_code=204)
    async def delete_user(username: str, request: Request) -> Response:
        if username == ADMIN:
            raise HTTPException(400, "Вбудованого адміністратора видалити не можна")
        if username == require_auth(request)["username"]:
            raise HTTPException(400, "Не можна видалити себе")
        if not users.remove(username):
            raise HTTPException(404, "Користувача не знайдено")
        return Response(status_code=204)

    def public(item: Dict[str, Any]) -> Dict[str, Any]:
        data = {key: item.get(key) for key in PUBLIC_KEYS}
        if item["status"] == "processing":
            data["progress"] = transcoder.progress.get(item["id"], 0.0)
        return data

    @router.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return (BASE_DIR / "templates" / "index.html").read_text(encoding="utf-8")

    @router.get("/api/videos")
    async def list_videos() -> Dict[str, Any]:
        usage = shutil.disk_usage(settings.data_dir)
        return {
            "videos": [public(item) for item in store.list()],
            "disk": {"free": usage.free, "total": usage.total},
            "max_upload": settings.max_upload_bytes,
        }

    @admin.post("/api/videos", status_code=201)
    async def upload_video(request: Request) -> Dict[str, Any]:
        name = clean_name(request.headers.get("x-filename", ""))
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

        # Місце потрібне і на оригінал, і на готовий файл після перекодування.
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
        }
        store.add(item)
        transcoder.enqueue(video_id)
        return public(item)

    @admin.post("/api/videos/{video_id}/retry")
    async def retry_video(video_id: str) -> Dict[str, Any]:
        item = store.get(video_id)
        if not item:
            raise HTTPException(404, "Відео не знайдено")
        if item["status"] != "error":
            raise HTTPException(409, "Повторити можна лише відео з помилкою")
        if not (settings.uploads_dir / item["source"]).is_file():
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
        # FileResponse сам обробляє Range-запити, потрібні для перемотування.
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

    @admin.delete("/api/videos/{video_id}", status_code=204)
    async def delete_video(video_id: str) -> Response:
        if not store.remove(video_id):
            raise HTTPException(404, "Відео не знайдено")
        transcoder.cancel(video_id)
        remove_files(settings, video_id)
        return Response(status_code=204)

    @app.websocket("/ws")
    async def websocket(ws: WebSocket) -> None:
        role = ws.query_params.get("role")
        await ws.accept()
        user = current_user(ws)
        if user is None:
            await ws.close(code=WS_UNAUTHORIZED)
            return
        ws.state.device_id = user.get("device_id")
        if role == "tv":
            await serve_tv(ws)
        elif role == "remote":
            await serve_remote(ws)
        else:
            await ws.close(code=1008)

    async def serve_tv(ws: WebSocket) -> None:
        # Проектор один: нове з'єднання стає активним, попереднє закривається.
        previous, hub.tv, hub.state = hub.tv, ws, None
        if previous is not None:
            with suppress(Exception):
                await previous.close(code=WS_REPLACED)
        await hub.broadcast({"type": "tv", "connected": True})
        try:
            while True:
                message = await ws.receive_json()
                if isinstance(message, dict) and message.get("type") == "state":
                    hub.state = clean_state(message)
                    await hub.broadcast({"type": "state", **hub.state})
        except WebSocketDisconnect:
            pass
        except Exception:  # сторінка надіслала щось, що не є JSON
            with suppress(Exception):
                await ws.close(code=1003)
        finally:
            if hub.tv is ws:
                hub.tv, hub.state = None, None
                await hub.broadcast({"type": "tv", "connected": False})

    async def serve_remote(ws: WebSocket) -> None:
        hub.remotes.add(ws)
        await hub.send(ws, {"type": "tv", "connected": hub.tv is not None})
        if hub.state is not None:
            await hub.send(ws, {"type": "state", **hub.state})
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
            with suppress(Exception):
                await ws.close(code=1003)
        finally:
            hub.remotes.discard(ws)

    app.include_router(router)
    app.include_router(admin)
    return app


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    app = create_app()
    # Один процес: черга і стан пульта живуть у пам'яті.
    uvicorn.run(app, host="0.0.0.0", port=app.state.settings.port)
