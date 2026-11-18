"""Puts the app together: the services, their start and stop, the routes."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from typing import Optional

from fastapi import FastAPI

from .assets import VersionedStatic
from .auth import NotAuthenticated
from .config import MIN_TOKEN_LENGTH, STATIC_DIR, Settings
from .routes import ROUTERS
from .routes.login import not_authenticated
from .services import Services


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or Settings.from_env()
    if len(settings.token) < MIN_TOKEN_LENGTH:
        raise RuntimeError(
            "Set PROMIN_TOKEN: at least %d characters" % MIN_TOKEN_LENGTH
        )
    svc = Services.create(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        for folder in (settings.uploads_dir, settings.videos_dir, settings.thumbs_dir):
            folder.mkdir(parents=True, exist_ok=True)
        # Half-written files are useless after a restart.
        for leftover in list(settings.uploads_dir.glob("*.part")) + list(
            settings.videos_dir.glob("*.tmp.mp4")
        ):
            leftover.unlink(missing_ok=True)
        svc.load()
        await svc.transcoder.start()
        yield
        if svc.tvtest.task:
            svc.tvtest.task.cancel()
            with suppress(asyncio.CancelledError):
                await svc.tvtest.task
        await svc.transcoder.stop()
        svc.close()

    app = FastAPI(title="Promin", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.services = svc
    app.mount("/static", VersionedStatic(directory=STATIC_DIR), name="static")
    app.add_exception_handler(NotAuthenticated, not_authenticated)
    for router in ROUTERS:
        app.include_router(router)
    return app
