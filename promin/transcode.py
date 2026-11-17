"""The worker that turns uploads into files the projector plays, one video at a time."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from contextlib import suppress
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import Settings
from .encoding import EncodingStore
from .ffmpeg import (
    ProcessingError, build_ffmpeg_args, detect_hdr_filter, plan_encode, reap, thumbnail_args,
    video_info,
)
from .library import VideoStore, remove_files

log = logging.getLogger(__name__)

# Version of the thumbnail method: after it changes, old thumbnails are remade.
THUMB_VERSION = 2

# How long ffmpeg and ffprobe may go without a sign of life before they are stopped.
FFMPEG_STALL_SECONDS = 10 * 60
PROBE_TIMEOUT_SECONDS = 2 * 60
THUMB_TIMEOUT_SECONDS = 2 * 60


class Transcoder:
    """A queue with a single worker: videos are processed one at a time."""

    def __init__(self, settings: Settings, videos: VideoStore, encoding: EncodingStore) -> None:
        self.settings = settings
        self.videos = videos
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
            item for item in self.videos.list() if item["status"] in ("queued", "processing")
        ]
        for item in sorted(pending, key=lambda item: item["created"]):
            self.videos.update(item["id"], status="queued", error=None)
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
        for item in self.videos.list():
            if item["status"] != "ready" or item.get("info"):
                continue
            video = self.settings.videos_dir / (item["id"] + ".mp4")
            if not video.is_file():
                continue
            info = await self._info(video)
            if info:
                self.videos.update(item["id"], info=info)

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
        for item in self.videos.list():
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
            self.videos.update(
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
                self.videos.update(video_id, status="error", error=str(exc)[-2000:])

    async def _process(self, video_id: str) -> None:
        item = self.videos.get(video_id)
        if not item or item["status"] != "queued":
            return
        settings = self.settings
        # A record without a source (a hand-edited list) fails below as a missing original.
        src = settings.uploads_dir / (item.get("source") or "missing")
        tmp = settings.videos_dir / (video_id + ".tmp.mp4")
        final = settings.videos_dir / (video_id + ".mp4")
        self._current_id = video_id
        self.progress[video_id] = 0.0
        self.videos.update(video_id, status="processing")
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

            if self.videos.get(video_id) is None:  # deleted while processing
                remove_files(settings, video_id)
                return
            if code != 0:
                raise ProcessingError(err.strip() or "ffmpeg завершився з кодом %d" % code)

            os.replace(tmp, final)
            thumb = await self._thumbnail(final, video_id, plan["duration"])
            info = await self._info(final)
            updated = self.videos.update(
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
            self.videos.update(video_id, status="error", error=text[-2000:])
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
            await reap(proc)
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
            await reap(proc)
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
            await reap(proc)
        if proc.returncode != 0 or not tmp.is_file():
            tmp.unlink(missing_ok=True)
            return False
        os.replace(tmp, out)
        return True
