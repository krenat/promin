"""The phone page and the videos: the list, upload, the files themselves, deletion."""
from __future__ import annotations

import shutil
from typing import Any, AsyncIterator, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.requests import ClientDisconnect

from ..library import clean_name
from ..upload import UploadError, check_upload, receive_upload
from .deps import ADMIN, AUTH, MEMBER, Svc, page

BATCH_DELETE_MAX = 500
# How each reason an upload is refused for reads in HTTP.
UPLOAD_STATUS = {
    "type": 415, "no_length": 411, "empty": 400, "too_large": 413, "no_space": 507,
    "interrupted": 400,
}

router = APIRouter()


class VideoIds(BaseModel):
    ids: List[str]


@router.get("/", dependencies=AUTH)
async def index() -> Response:
    return page("index.html")


@router.get("/api/videos", dependencies=AUTH)
async def list_videos(svc: Svc) -> Dict[str, Any]:
    usage = shutil.disk_usage(svc.settings.data_dir)
    videos = [svc.video_view(item) for item in svc.videos.list()]
    counts: Dict[Optional[str], int] = {}
    for video in videos:
        counts[video["folder"]] = counts.get(video["folder"], 0) + 1
    return {
        "videos": videos,
        "folders": [dict(f, count=counts.get(f["id"], 0)) for f in svc.folders.list()],
        "disk": {"free": usage.free, "total": usage.total},
        "max_upload": svc.settings.max_upload_bytes,
    }


async def upload_body(request: Request) -> AsyncIterator[bytes]:
    """The body of an upload as it arrives; a phone that goes away is an interrupted upload."""
    try:
        async for chunk in request.stream():
            yield chunk
    except ClientDisconnect:
        raise UploadError("interrupted", "Завантаження обірвалося")


@router.post("/api/videos", status_code=201, dependencies=MEMBER)
async def upload_video(request: Request, svc: Svc) -> Dict[str, Any]:
    name = clean_name(request.headers.get("x-filename", ""))
    # The folder the phone is looking at; one deleted meanwhile means no folder.
    folder = request.headers.get("x-folder") or None
    if not svc.folders.exists(folder):
        folder = None
    try:
        length = check_upload(svc.settings, name, request.headers.get("content-length"))
        item = await receive_upload(svc.settings, upload_body(request), name, length, folder)
    except UploadError as exc:
        raise HTTPException(UPLOAD_STATUS[exc.reason], str(exc))
    svc.videos.add(item)
    svc.transcoder.enqueue(item["id"])
    return svc.video_view(item)


@router.post("/api/videos/{video_id}/retry", dependencies=MEMBER)
async def retry_video(video_id: str, svc: Svc) -> Dict[str, Any]:
    item = svc.videos.get(video_id)
    if not item:
        raise HTTPException(404, "Відео не знайдено")
    if item["status"] != "error":
        raise HTTPException(409, "Повторити можна лише відео з помилкою")
    if not item.get("source") or not (svc.settings.uploads_dir / item["source"]).is_file():
        raise HTTPException(409, "Оригінал не зберігся, завантажте відео ще раз")
    svc.videos.update(video_id, status="queued", error=None)
    svc.transcoder.enqueue(video_id)
    return svc.video_view(svc.videos.get(video_id) or item)


@router.get("/media/{video_id}", dependencies=AUTH)
async def media(video_id: str, svc: Svc) -> FileResponse:
    item = svc.videos.get(video_id)
    path = svc.settings.videos_dir / (video_id + ".mp4")
    if not item or item["status"] != "ready" or not path.is_file():
        raise HTTPException(404, "Відео не знайдено")
    # FileResponse handles Range requests itself; seeking needs them.
    return FileResponse(path, media_type="video/mp4")


@router.get("/thumb/{video_id}", dependencies=AUTH)
async def thumb(video_id: str, svc: Svc) -> FileResponse:
    item = svc.videos.get(video_id)
    path = svc.settings.thumbs_dir / (video_id + ".jpg")
    if not item or not path.is_file():
        raise HTTPException(404, "Мініатюру не знайдено")
    return FileResponse(
        path, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"}
    )


@router.delete("/api/videos/{video_id}", status_code=204, dependencies=MEMBER)
async def delete_video(video_id: str, svc: Svc) -> Response:
    if not await svc.delete_videos([video_id]):
        raise HTTPException(404, "Відео не знайдено")
    return Response(status_code=204)


@router.post("/api/videos/delete", dependencies=ADMIN)
async def delete_videos(body: VideoIds, svc: Svc) -> Dict[str, Any]:
    """Deletes several videos at once. Ids that are already gone are reported, not an error."""
    ids = list(dict.fromkeys(body.ids))  # without repeats, in the given order
    if not ids:
        raise HTTPException(400, "Не вибрано жодного відео")
    if len(ids) > BATCH_DELETE_MAX:
        raise HTTPException(400, "За раз можна видалити щонайбільше %d відео" % BATCH_DELETE_MAX)
    deleted = await svc.delete_videos(ids)
    gone = set(deleted)
    return {"deleted": deleted, "missing": [i for i in ids if i not in gone]}
