"""Folders of the library and moving a video between them."""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from ..library import clean_folder_name
from .deps import MEMBER, Svc

router = APIRouter()


class FolderName(BaseModel):
    name: str


class VideoFolder(BaseModel):
    folder: Optional[str] = None


def folder_name(raw: str) -> str:
    try:
        return clean_folder_name(raw)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/api/folders", status_code=201, dependencies=MEMBER)
async def create_folder(body: FolderName, svc: Svc) -> Dict[str, Any]:
    name = folder_name(body.name)
    try:
        folder_id = svc.folders.add(name)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return {"id": folder_id, "name": name}


@router.patch("/api/folders/{folder_id}", dependencies=MEMBER)
async def rename_folder(folder_id: str, body: FolderName, svc: Svc) -> Dict[str, Any]:
    name = folder_name(body.name)
    try:
        if not svc.folders.rename(folder_id, name):
            raise HTTPException(404, "Теку не знайдено")
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return {"id": folder_id, "name": name}


@router.delete("/api/folders/{folder_id}", status_code=204, dependencies=MEMBER)
async def delete_folder(folder_id: str, svc: Svc) -> Response:
    """Deletes a folder; its videos stay in the library without a folder."""
    if not svc.folders.remove(folder_id):
        raise HTTPException(404, "Теку не знайдено")
    svc.videos.clear_folder(folder_id)
    return Response(status_code=204)


@router.post("/api/videos/{video_id}/folder", dependencies=MEMBER)
async def move_video(video_id: str, body: VideoFolder, svc: Svc) -> Dict[str, Any]:
    if body.folder is not None and not svc.folders.exists(body.folder):
        raise HTTPException(404, "Теку не знайдено")
    if not svc.videos.update(video_id, folder=body.folder):
        raise HTTPException(404, "Відео не знайдено")
    item = svc.videos.get(video_id)
    if item is None:  # deleted a moment ago
        raise HTTPException(404, "Відео не знайдено")
    return svc.video_view(item)
