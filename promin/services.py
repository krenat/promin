"""Everything one running app is made of, and the operations that span several parts."""
from __future__ import annotations

import logging
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Dict, List

from .accounts import SessionStore, UserStore
from .auth import Auth, LoginLimiter
from .config import Settings
from .devices import PAIR_CODE_TTL, PAIR_STARTS_PER_WINDOW, DeviceStore, Pairing
from .encoding import Encoding, EncodingStore
from .library import PUBLIC_KEYS, FolderStore, QueueStore, VideoStore, remove_files
from .playback import Playback
from .hub import WS_UNAUTHORIZED, Hub
from .transcode import Transcoder
from .tvtest import TvTest

log = logging.getLogger(__name__)


@dataclass
class Services:
    settings: Settings
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
    def create(cls, settings: Settings) -> "Services":
        data = settings.data_dir
        videos = VideoStore(data / "videos.json")
        queue = QueueStore(data / "queue.json")
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
        encoding = EncodingStore(data / "encoding.json", defaults)
        users = UserStore(data / "users.json")
        sessions = SessionStore(data / "sessions.json")
        devices = DeviceStore(data / "devices.json")
        return cls(
            settings=settings,
            videos=videos,
            folders=FolderStore(data / "folders.json"),
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
            tvtest=TvTest(data / "tvtest.json"),
        )

    def load(self) -> None:
        """Reads every data file; called once, as the app starts."""
        for part in (
            self.videos, self.encoding, self.users, self.sessions, self.devices, self.queue,
            self.folders, self.tvtest,
        ):
            part.load()

    # ------------------------------------------------------------------ videos

    def video_view(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """A video as the pages see it."""
        data = {key: item.get(key) for key in PUBLIC_KEYS}
        # A folder that is gone (deleted, or its file damaged) reads as no folder.
        if not self.folders.exists(data["folder"]):
            data["folder"] = None
        if item["status"] == "processing":
            data["progress"] = self.transcoder.progress.get(item["id"], 0.0)
        return data

    def delete_videos(self, video_ids: List[str]) -> List[str]:
        """Deletes videos and returns the ids that existed. The list is written once, however
        many videos go: every write waits for the disk and holds up everything else."""
        # Files are touched only for ids the list knew, so an id never reaches the glob raw.
        deleted = self.videos.remove_many(video_ids)
        for video_id in deleted:
            self.transcoder.cancel(video_id)
            remove_files(self.settings, video_id)
        return deleted

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
