"""Promin — домашній відеосервер для проектора.

Список відео, завантаження, перекодування, перегляд, видалення і вхід за токеном.
Пульт для проектора (етап 4) ще не доданий.
"""
from __future__ import annotations

import asyncio
import hmac
import html
import json
import logging
import os
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
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.requests import ClientDisconnect

log = logging.getLogger("promin")

BASE_DIR = Path(__file__).resolve().parent
MB = 1024 * 1024

ALLOWED_EXT = {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".3gp"}
PUBLIC_KEYS = ("id", "name", "size", "status", "created", "duration", "thumb", "error")

COOKIE_NAME = "promin_session"
COOKIE_MAX_AGE = 365 * 24 * 3600
MIN_TOKEN_LENGTH = 12
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
    """За виводом ffprobe вирішує: перепакувати файл чи перекодувати."""
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

    # Перепаковуємо лише те, що проектор точно зіграє як є.
    remux = (
        video.get("codec_name") == "h264"
        and video.get("pix_fmt") == "yuv420p"
        and not hdr
        and stream_rotation(video) == 0
        and 0 < int(video.get("width") or 0) <= MAX_WIDTH
        and 0 < int(video.get("height") or 0) <= MAX_HEIGHT
        and 0 < fps <= MAX_FPS + 0.5
        and 0 < bitrate <= settings.max_bitrate_kbps * 1000 * 1.25
        and (audio is None or audio.get("codec_name") == "aac")
    )
    return {
        "mode": "remux" if remux else "transcode",
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

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task

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
        proc = await asyncio.create_subprocess_exec(
            self.ffmpeg, "-nostdin", "-y", "-v", "error",
            "-ss", "%.2f" % min(1.0, duration / 2), "-i", str(video),
            "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "5", str(out),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await proc.wait()
        finally:
            await self._reap(proc)
        return proc.returncode == 0 and out.is_file()


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
    # У cookie лежить не сам токен, а похідне від нього значення.
    session_value = hmac.new(
        settings.token.encode("utf-8"), b"promin-session", "sha256"
    ).hexdigest()
    limiter = LoginLimiter()
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
        await transcoder.start()
        yield
        await transcoder.stop()

    app = FastAPI(title="Promin", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.store = store
    app.state.transcoder = transcoder
    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

    def is_authenticated(request: Request) -> bool:
        return same(request.cookies.get(COOKIE_NAME, ""), session_value)

    def require_auth(request: Request) -> None:
        if not is_authenticated(request):
            raise NotAuthenticated()

    @app.exception_handler(NotAuthenticated)
    async def not_authenticated(request: Request, exc: NotAuthenticated) -> Response:
        if request.method == "GET" and not request.url.path.startswith("/api/"):
            return RedirectResponse("/login", status_code=303)
        return JSONResponse({"detail": "Потрібен вхід"}, status_code=401)

    def login_page(error: str = "", status_code: int = 200) -> HTMLResponse:
        page = (BASE_DIR / "templates" / "login.html").read_text(encoding="utf-8")
        return HTMLResponse(page.replace("{{error}}", html.escape(error)), status_code)

    @app.get("/login")
    async def login_form(request: Request) -> Response:
        if is_authenticated(request):
            return RedirectResponse("/", status_code=303)
        return login_page()

    @app.post("/login")
    async def login(request: Request) -> Response:
        ip = request.client.host if request.client else "unknown"
        if limiter.blocked(ip):
            return login_page("Забагато спроб. Спробуйте знову за 15 хвилин.", 429)
        if int(request.headers.get("content-length") or 0) > 4096:
            return login_page("Токен задовгий.", 413)
        form = parse_qs((await request.body()).decode("utf-8", errors="replace"))
        token = (form.get("token") or [""])[0].strip()
        if not same(token, settings.token):
            limiter.fail(ip)
            return login_page("Неправильний токен.", 401)
        limiter.reset(ip)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            COOKIE_NAME, session_value, max_age=COOKIE_MAX_AGE, httponly=True, samesite="lax"
        )
        return response

    # Усе нижче доступне лише після входу, включно з відеофайлами.
    router = APIRouter(dependencies=[Depends(require_auth)])

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

    @router.post("/api/videos", status_code=201)
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

    @router.post("/api/videos/{video_id}/retry")
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

    @router.delete("/api/videos/{video_id}", status_code=204)
    async def delete_video(video_id: str) -> Response:
        if not store.remove(video_id):
            raise HTTPException(404, "Відео не знайдено")
        transcoder.cancel(video_id)
        remove_files(settings, video_id)
        return Response(status_code=204)

    app.include_router(router)
    return app


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    app = create_app()
    # Один процес: черга і стан пульта живуть у пам'яті.
    uvicorn.run(app, host="0.0.0.0", port=app.state.settings.port)
