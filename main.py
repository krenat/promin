"""Promin — домашній відеосервер для проектора.

Етапи 1–2: список відео, завантаження, перегляд і видалення.
Перекодування (етап 3), пульт (етап 4) і вхід за токеном (етап 5) ще не додані.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import unquote

import anyio
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.requests import ClientDisconnect

BASE_DIR = Path(__file__).resolve().parent

# Розширення, які приймаємо, і тип, з яким віддаємо файл браузеру.
MEDIA_TYPES = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/mp4",
    ".webm": "video/webm",
}

MB = 1024 * 1024


@dataclass
class Settings:
    data_dir: Path
    port: int = 8080
    max_upload_bytes: int = 4096 * MB
    # Скільки вільного місця має лишитися на диску після завантаження.
    disk_reserve_bytes: int = 500 * MB

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            data_dir=Path(os.environ.get("PROMIN_DATA_DIR", "./data")).resolve(),
            port=int(os.environ.get("PROMIN_PORT", "8080")),
            max_upload_bytes=int(os.environ.get("PROMIN_MAX_UPLOAD_MB", "4096")) * MB,
        )

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def videos_dir(self) -> Path:
        return self.data_dir / "videos"


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

    def remove(self, video_id: str) -> bool:
        with self._lock:
            if video_id not in self._items:
                return False
            del self._items[video_id]
            self._save()
            return True


def clean_name(raw: str) -> str:
    """Назва для показу: без шляху, обмеженої довжини."""
    name = unquote(raw).replace("\\", "/").split("/")[-1].strip()
    return name[:200] or "video"


def remove_files(settings: Settings, video_id: str) -> None:
    for folder in (settings.uploads_dir, settings.videos_dir):
        for path in folder.glob(video_id + ".*"):
            path.unlink(missing_ok=True)


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or Settings.from_env()
    store = Store(settings.data_dir / "videos.json")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.uploads_dir.mkdir(parents=True, exist_ok=True)
        settings.videos_dir.mkdir(parents=True, exist_ok=True)
        # Недописані завантаження після перезапуску нікому не потрібні.
        for part in settings.uploads_dir.glob("*.part"):
            part.unlink(missing_ok=True)
        store.load()
        yield

    app = FastAPI(title="Promin", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.store = store
    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return (BASE_DIR / "templates" / "index.html").read_text(encoding="utf-8")

    @app.get("/api/videos")
    async def list_videos() -> Dict[str, Any]:
        usage = shutil.disk_usage(settings.data_dir)
        videos = [
            {key: item[key] for key in ("id", "name", "size", "status", "created")}
            for item in store.list()
        ]
        return {
            "videos": videos,
            "disk": {"free": usage.free, "total": usage.total},
            "max_upload": settings.max_upload_bytes,
        }

    @app.post("/api/videos", status_code=201)
    async def upload_video(request: Request) -> Dict[str, Any]:
        name = clean_name(request.headers.get("x-filename", ""))
        ext = Path(name).suffix.lower()
        if ext not in MEDIA_TYPES:
            raise HTTPException(415, "Підтримуються лише відео MP4, MOV, M4V і WebM")

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

        # На етапі 3 тут з'явиться черга перекодування; поки файл одразу готовий.
        filename = video_id + ext
        os.replace(part, settings.videos_dir / filename)
        item = {
            "id": video_id,
            "name": name,
            "size": length,
            "status": "ready",
            "created": time.time(),
            "file": filename,
        }
        store.add(item)
        return {key: item[key] for key in ("id", "name", "size", "status", "created")}

    @app.get("/media/{video_id}")
    async def media(video_id: str) -> FileResponse:
        item = store.get(video_id)
        if not item or item["status"] != "ready":
            raise HTTPException(404, "Відео не знайдено")
        path = settings.videos_dir / item["file"]
        if not path.is_file():
            raise HTTPException(404, "Файл відео відсутній")
        # FileResponse сам обробляє Range-запити, потрібні для перемотування.
        return FileResponse(path, media_type=MEDIA_TYPES[path.suffix])

    @app.delete("/api/videos/{video_id}", status_code=204)
    async def delete_video(video_id: str) -> Response:
        if not store.remove(video_id):
            raise HTTPException(404, "Відео не знайдено")
        remove_files(settings, video_id)
        return Response(status_code=204)

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    # Один процес: черга і стан пульта житимуть у пам'яті.
    uvicorn.run(app, host="0.0.0.0", port=app.state.settings.port)
