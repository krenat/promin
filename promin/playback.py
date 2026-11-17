"""What the projector plays next: the watch queue as phones see it, and moving along it."""
from __future__ import annotations

from typing import Any, Dict, List

from .hub import Hub
from .library import QueueStore, VideoStore


class Playback:
    def __init__(self, hub: Hub, queue: QueueStore, videos: VideoStore) -> None:
        self.hub = hub
        self.queue = queue
        self.videos = videos

    def queue_view(self) -> List[Dict[str, Any]]:
        """The queue as phones show it: only videos that can still be played."""
        items = []
        for entry in self.queue.list():
            item = self.videos.get(entry["video_id"])
            if item and item["status"] == "ready":
                items.append({
                    "entry": entry["entry"], "video_id": item["id"], "name": item["name"],
                    "duration": item.get("duration"), "thumb": bool(item.get("thumb")),
                    "thumb_v": item.get("thumb_v"),
                })
        return items

    async def queue_changed(self) -> Dict[str, Any]:
        """Tells every remote about the new queue; returns it as an API response."""
        view = self.queue_view()
        await self.hub.broadcast({"type": "queue", "queue": view})
        return {"queue": view}

    async def forget_videos(self, video_ids: set) -> None:
        """Drops deleted videos from the queue and tells the remotes if it changed."""
        if self.queue.remove_videos(video_ids):
            await self.queue_changed()

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
