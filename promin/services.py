"""Everything one running app is made of, and the operations that span several parts."""
from __future__ import annotations

import logging
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set

from .accounts import SessionStore, UserStore
from .auth import Auth, LoginLimiter
from .config import Settings
from .db import DB_NAME, Database
from .devices import PAIR_CODE_TTL, PAIR_STARTS_PER_WINDOW, DeviceStore, Pairing
from .encoding import Encoding, EncodingStore
from .hub import WS_UNAUTHORIZED, Hub
from .legacy import import_json
from .library import PUBLIC_KEYS, FolderStore, QueueStore, Video, VideoStore, remove_files
from .playback import Playback
from .transcode import Transcoder
from .tvtest import TvTest

log = logging.getLogger(__name__)


@dataclass
class Services:
    settings: Settings
    db: Database
    videos: VideoStore
    folders: FolderStore
    queue: QueueStore
    encoding: EncodingStore
    transcoder: Transcoder
    users: UserStore
    sessions: SessionStore
    devices: DeviceStore
    auth: Auth
    pairing: Pairing
    limiter: LoginLimiter
    pair_start_limiter: LoginLimiter
    hub: Hub
    playback: Playback
    tvtest: TvTest

    @classmethod
    def create(cls, settings: Settings) -> Services:
        db = Database(settings.data_dir / DB_NAME)
        videos = VideoStore(db)
        queue = QueueStore(db)
        hub = Hub()
        # A wrong value in the environment must not stop the service either.
        defaults = Encoding(
            max_bitrate_kbps=settings.max_bitrate_kbps, x264_preset=settings.x264_preset
        )
        if defaults.problem():
            log.warning(
                "Ignoring PROMIN_MAX_BITRATE_KBPS and PROMIN_X264_PRESET: %s", defaults.problem()
            )
            defaults = Encoding()
        encoding = EncodingStore(db, defaults)
        users = UserStore(db)
        sessions = SessionStore(db)
        devices = DeviceStore(db)
        return cls(
            settings=settings,
            db=db,
            videos=videos,
            folders=FolderStore(db),
            queue=queue,
            encoding=encoding,
            transcoder=Transcoder(settings, videos, encoding),
            users=users,
            sessions=sessions,
            devices=devices,
            auth=Auth(settings.token, users, sessions, devices),
            pairing=Pairing(),
            limiter=LoginLimiter(),
            pair_start_limiter=LoginLimiter(PAIR_STARTS_PER_WINDOW, PAIR_CODE_TTL),
            hub=hub,
            playback=Playback(hub, queue, videos),
            tvtest=TvTest(db),
        )

    def load(self) -> None:
        """Opens the data; called once, as the app starts."""
        self.db.open()
        import_json(self.settings.data_dir, self.db)
        for part in (self.encoding, self.devices, self.tvtest):
            part.load()

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------------ videos

    def video_view(self, item: Video, folders: Optional[Set[str]] = None) -> Dict[str, Any]:
        """A video as the pages see it. A caller with many videos passes the ids of the
        folders that exist, so they are read once and not once per video."""
        data: Dict[str, Any] = {key: item.get(key) for key in PUBLIC_KEYS}
        known = self.folders.exists(item["folder"]) if folders is None else item["folder"] in folders
        # A folder that is gone reads as no folder.
        if not known:
            data["folder"] = None
        if item["status"] == "processing":
            data["progress"] = self.transcoder.progress.get(item["id"], 0.0)
        return data

    async def delete_videos(self, video_ids: List[str]) -> List[str]:
        """Deletes videos and returns the ids that existed. They leave the list and the
        watch queue together: there is no moment when the queue names a video that is gone."""
        with self.db.transaction():
            deleted = self.videos.remove_many(video_ids)
            queue_changed = self.queue.remove_videos(set(deleted))
        # Files are touched only for ids the list knew, so an id never reaches the glob raw.
        for video_id in deleted:
            self.transcoder.cancel(video_id)
            remove_files(self.settings, video_id)
        if queue_changed:
            await self.playback.queue_changed()
        return deleted

    def delete_folder(self, folder_id: str) -> bool:
        """Deletes a folder; its videos stay in the library without one. False if there is
        no such folder."""
        with self.db.transaction():
            if not self.folders.remove(folder_id):
                return False
            self.videos.clear_folder(folder_id)
            return True

    # ------------------------------------------------------------- connections

    async def drop_lost_connections(self) -> None:
        """Closes live connections whose login stopped working: a deleted user, a changed
        password, a logout or a disconnected projector. Otherwise an open remote would keep
        working until the phone reconnects."""
        hub = self.hub
        for ws in list(hub.remotes) + ([hub.tv] if hub.tv is not None else []):
            if self.auth.current_user(ws) is None:
                with suppress(Exception):
                    await ws.close(code=WS_UNAUTHORIZED)
