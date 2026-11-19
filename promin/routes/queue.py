from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .deps import MEMBER, Svc

router = APIRouter()


class QueueAdd(BaseModel):
    video_id: str


class QueueMove(BaseModel):
    position: int


@router.get("/api/queue", dependencies=MEMBER)
async def get_queue(svc: Svc) -> Dict[str, Any]:
    return {"queue": svc.playback.queue_view()}


@router.post("/api/queue", status_code=201, dependencies=MEMBER)
async def add_to_queue(body: QueueAdd, svc: Svc) -> Dict[str, Any]:
    item = svc.videos.get(body.video_id)
    if not item or item["status"] != "ready":
        raise HTTPException(404, "Це відео недоступне")
    try:
        svc.queue.add(item["id"])
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return await svc.playback.queue_changed()


@router.delete("/api/queue/{entry}", dependencies=MEMBER)
async def remove_from_queue(entry: str, svc: Svc) -> Dict[str, Any]:
    if not svc.queue.remove(entry):
        raise HTTPException(404, "Цього відео вже немає в черзі")
    return await svc.playback.queue_changed()


@router.post("/api/queue/{entry}/move", dependencies=MEMBER)
async def move_in_queue(entry: str, body: QueueMove, svc: Svc) -> Dict[str, Any]:
    if not svc.queue.move(entry, body.position):
        raise HTTPException(404, "Цього відео вже немає в черзі")
    return await svc.playback.queue_changed()


@router.delete("/api/queue", dependencies=MEMBER)
async def clear_queue(svc: Svc) -> Dict[str, Any]:
    svc.queue.clear()
    return await svc.playback.queue_changed()
