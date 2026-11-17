"""The projector check: starting it, its progress, and the files the projector downloads."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from functools import lru_cache
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import FileResponse

from ..services import Services
from ..tvtest import (
    TVTEST_MAX_DROPPED, TVTEST_MEASURE_SECONDS, TVTEST_MIN_ADVANCE, TVTEST_SPEED_CHUNK,
    TVTEST_SPEED_SECONDS, make_tvtest_clip, tvtest_clip_name,
)
from .deps import AUTH, MEMBER, Svc

log = logging.getLogger(__name__)

router = APIRouter()


async def run_tvtest(svc: Services, run: str) -> None:
    """Generates the clips that are missing, then hands the check to the projector."""
    tvtest, hub = svc.tvtest, svc.hub
    folder = svc.settings.tvtest_dir
    try:
        folder.mkdir(parents=True, exist_ok=True)
        for level in tvtest.levels:
            await make_tvtest_clip(svc.transcoder.ffmpeg or "", level, folder)
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


@router.get("/api/tvtest", dependencies=MEMBER)
async def get_tvtest(svc: Svc) -> Dict[str, Any]:
    return svc.tvtest.view()


@router.post("/api/tvtest", status_code=202, dependencies=MEMBER)
async def start_tvtest(svc: Svc) -> Dict[str, Any]:
    tvtest, hub = svc.tvtest, svc.hub
    if tvtest.view()["status"] != "idle":
        raise HTTPException(409, "Перевірка вже триває")
    if not svc.transcoder.ffmpeg:
        raise HTTPException(503, "На сервері не встановлено ffmpeg")
    if hub.tv is None:
        raise HTTPException(409, "Проектор не на зв'язку: відкрийте на ньому сторінку /tv")
    if hub.state is None or not hub.state["started"]:
        raise HTTPException(409, "Спершу натисніть «Почати» на проекторі")
    if hub.state["video_id"]:
        raise HTTPException(409, "Спершу зупиніть відео на проекторі")
    tvtest.task = asyncio.create_task(run_tvtest(svc, tvtest.begin()))
    return tvtest.view()


@router.get("/tvtest/clip/{level_id}", dependencies=AUTH)
async def tvtest_clip(level_id: str, svc: Svc) -> FileResponse:
    # The path is built from a known level, so the id never reaches the disk raw.
    level = next((lv for lv in svc.tvtest.levels if lv["id"] == level_id), None)
    path = svc.settings.tvtest_dir / tvtest_clip_name(level) if level else None
    if path is None or not path.is_file():
        raise HTTPException(404, "Ролик не знайдено")
    return FileResponse(path, media_type="video/mp4", headers={"Cache-Control": "no-store"})


@lru_cache(maxsize=1)
def speed_payload() -> bytes:
    """Random bytes for the speed measurement, made on first use."""
    return os.urandom(TVTEST_SPEED_CHUNK)


@router.get("/tvtest/speed", dependencies=AUTH)
async def tvtest_speed() -> Response:
    """Bytes that cannot be compressed: the projector times how fast they arrive."""
    return Response(
        speed_payload(), media_type="application/octet-stream",
        headers={"Cache-Control": "no-store"},
    )
