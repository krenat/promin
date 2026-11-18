"""What the projector plays next: the watch queue as phones see it, and moving along it."""
from __future__ import annotations

from typing import Any, Dict, List

from .hub import Hub
from .library import QueuedVideo, QueueStore, VideoStore


class Playback:
    def __init__(self, hub: Hub, queue: QueueStore, videos: VideoStore) -> None:
        self.hub = hub
        self.queue = queue
        self.videos = videos

    def queue_view(self) -> List[QueuedVideo]:
        """The queue as phones show it: only videos that can still be played."""
        return self.queue.playable()

    async def queue_changed(self) -> Dict[str, Any]:
        """Tells every remote about the new queue; returns it as an API response."""
        view = self.queue_view()
        await self.hub.broadcast({"type": "queue", "queue": view})
        return {"queue": view}

    async def play_next(self) -> bool:
        """Loads the first playable queue entry on the projector; False if there is none."""
        hub = self.hub
        if hub.tv is None:
            return False
        popped = played = False
        while not played:
            entry = self.queue.pop()
            if entry is None:
                break
            popped = True
            item = self.videos.get(entry["video_id"])
            if item and item["status"] == "ready":  # skip videos deleted since they were queued
                await hub.send(hub.tv, {
                    "type": "command", "action": "load", "video_id": item["id"], "name": item["name"],
                })
                played = True
        if popped:
            await self.queue_changed()
        return played
