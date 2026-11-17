"""The remote: the hub between the projector page and the phones."""
from __future__ import annotations

from contextlib import suppress
from typing import Any, Dict, Optional

from fastapi import WebSocket

from .parsing import finite

# Which commands the remote may send to the projector.
REMOTE_ACTIONS = ("load", "play", "pause", "seek", "seek_by", "volume", "stop", "next")
WS_REPLACED = 4000  # the projector page was opened elsewhere
WS_UNAUTHORIZED = 4401


class Hub:
    """Connects remotes to the projector. State lives in the memory of one process."""

    def __init__(self) -> None:
        self.tv: Optional[WebSocket] = None
        self.remotes: set = set()
        self.state: Optional[Dict[str, Any]] = None

    @staticmethod
    async def send(ws: WebSocket, message: Dict[str, Any]) -> None:
        with suppress(Exception):  # the connection may have just dropped
            await ws.send_json(message)

    async def broadcast(self, message: Dict[str, Any]) -> None:
        for ws in list(self.remotes):
            await self.send(ws, message)


def clean_state(message: Dict[str, Any]) -> Dict[str, Any]:
    """State from the projector page: only known fields of known types."""

    def number(key: str) -> float:
        return finite(message.get(key))

    video_id = message.get("video_id")
    name = message.get("name")
    return {
        "video_id": str(video_id)[:64] if video_id else None,
        "name": str(name)[:200] if name else None,
        "position": number("position"),
        "duration": number("duration"),
        "volume": max(0.0, min(number("volume"), 1.0)),
        "paused": bool(message.get("paused")),
        "started": bool(message.get("started")),
    }
