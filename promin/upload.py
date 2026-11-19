"""Taking a video in: what may be uploaded, and writing it to disk as it arrives."""
from __future__ import annotations

import os
import shutil
import time
import uuid
from pathlib import Path
from typing import AsyncIterator, Optional

import anyio

from .config import Settings
from .library import ALLOWED_EXT, Video


class UploadError(Exception):
    """An upload that is not accepted. `reason` is one of REASONS; the text is shown to the
    user."""

    def __init__(self, reason: str, message: str) -> None:
        assert reason in REASONS, reason
        super().__init__(message)
        self.reason = reason


REASONS = ("type", "no_length", "empty", "too_large", "no_space", "interrupted")


def check_upload(settings: Settings, name: str, content_length: Optional[str]) -> int:
    """Decides before a byte is read whether the upload is taken; returns its size."""
    if Path(name).suffix.lower() not in ALLOWED_EXT:
        raise UploadError("type", "Підтримуються лише відео MP4, MOV, M4V, MKV, WebM і 3GP")
    try:
        length = int(content_length)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise UploadError("no_length", "Потрібен заголовок Content-Length")
    if length <= 0:
        raise UploadError("empty", "Порожній файл")
    if length > settings.max_upload_bytes:
        raise UploadError("too_large", "Файл більший за дозволений розмір")
    # Space is needed for both the original and the transcoded file.
    free = shutil.disk_usage(settings.data_dir).free
    if free < length * 2 + settings.disk_reserve_bytes:
        raise UploadError("no_space", "На сервері недостатньо вільного місця")
    return length


async def receive_upload(
    settings: Settings, chunks: AsyncIterator[bytes], name: str, length: int,
    folder: Optional[str],
) -> Video:
    """Writes the upload to disk and returns the record of the new video, ready to be queued.

    An upload that breaks off or does not match its declared size leaves no file behind.
    """
    video_id = uuid.uuid4().hex
    part = settings.uploads_dir / (video_id + ".part")
    received = 0
    try:
        async with await anyio.open_file(part, "wb") as f:
            async for chunk in chunks:
                received += len(chunk)
                if received > length:
                    raise UploadError("too_large", "Файл більший за заявлений розмір")
                await f.write(chunk)
        if received != length:
            raise UploadError("interrupted", "Завантаження обірвалося")
    except BaseException:  # whatever stopped it, the stream breaking off included
        part.unlink(missing_ok=True)
        raise

    source = video_id + Path(name).suffix.lower()
    os.replace(part, settings.uploads_dir / source)
    return {
        "id": video_id,
        "name": name,
        "size": length,
        "status": "queued",
        "created": time.time(),
        "source": source,
        "duration": None,
        "thumb": False,
        "thumb_v": None,
        "error": None,
        "folder": folder,
        "info": None,
    }
