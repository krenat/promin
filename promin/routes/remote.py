"""The projector page and the WebSocket that joins it to the remotes."""
from __future__ import annotations

import logging
from contextlib import suppress
from typing import Any, Dict
from urllib.parse import urlsplit

from fastapi import APIRouter, Response, WebSocket, WebSocketDisconnect

from ..hub import REMOTE_ACTIONS, WS_DISMISSED, WS_REPLACED, WS_UNAUTHORIZED, clean_state
from ..services import Services
from .deps import page, services

log = logging.getLogger(__name__)

router = APIRouter()


# The projector page is open to everyone: without a login it only shows a QR code to pair.
@router.get("/tv")
async def tv_page() -> Response:
    return page("tv.html")


@router.websocket("/ws")
async def websocket(ws: WebSocket) -> None:
    svc = services(ws)
    role = ws.query_params.get("role")
    # A browser always names the page that opens the connection. A foreign one (including
    # another port of this server, which receives the cookie too) is refused.
    origin = ws.headers.get("origin")
    if origin is not None and urlsplit(origin).netloc != ws.headers.get("host"):
        await ws.close(code=1008)
        return
    await ws.accept()
    user = svc.auth.current_user(ws)
    if user is None:
        await ws.close(code=WS_UNAUTHORIZED)
        return
    ws.state.device_id = user.get("device_id")
    if role == "tv":
        await serve_tv(svc, ws)
    elif role == "remote" and user["role"] != "device":  # a projector only shows
        await serve_remote(svc, ws)
    else:
        await ws.close(code=1008)


# ---------------------------------------------------------------- the projector


async def serve_tv(svc: Services, ws: WebSocket) -> None:
    hub, tvtest = svc.hub, svc.tvtest
    # There is one projector: a new connection takes over and the previous one is closed.
    previous, hub.tv, hub.state = hub.tv, ws, None
    if tvtest.status == "running":  # the page that was running the check is gone
        tvtest.fail("Сторінку проектора відкрили заново під час перевірки")
    if previous is not None:
        with suppress(Exception):
            await previous.close(code=WS_REPLACED)
    await hub.broadcast({"type": "tv", "connected": True})
    try:
        while True:
            message = await ws.receive_json()
            if isinstance(message, dict):
                await tv_message(svc, message)
    except WebSocketDisconnect:
        pass
    except Exception:  # the page sent something that is not JSON, or handling it failed
        log.exception("The projector connection failed")
        with suppress(Exception):
            await ws.close(code=1003)
    finally:
        if hub.tv is ws:
            hub.tv, hub.state = None, None
            if tvtest.status == "running":
                tvtest.fail("Проектор від'єднався під час перевірки")
            await hub.broadcast({"type": "tv", "connected": False})


async def tv_message(svc: Services, message: Dict[str, Any]) -> None:
    """One message from the projector page: its state, the end of a video, a check report."""
    hub = svc.hub
    kind = message.get("type")
    if kind == "state":
        hub.state = clean_state(message)
        await hub.broadcast({"type": "state", **hub.state})
    elif kind == "ended":
        # Only the video that is playing can end: a late or repeated report
        # must not skip the next one in the queue.
        playing = hub.state.get("video_id") if hub.state else None
        if playing and message.get("video_id") == playing:
            await svc.playback.play_next()
    elif kind == "test":
        svc.tvtest.report(message)


async def dismiss_tv(svc: Services) -> None:
    """Lets the projector go at a phone's request: whatever screen has /tv open stops being
    the projector until someone there chooses to show again. A paired projector stays
    paired; taking that away is done on the users page."""
    hub, tvtest = svc.hub, svc.tvtest
    tv, hub.tv, hub.state = hub.tv, None, None
    if tv is None:
        return
    if tvtest.status == "running":
        tvtest.fail("Проектор від'єднали під час перевірки")
    with suppress(Exception):
        await tv.close(code=WS_DISMISSED)
    # Another screen may have become the projector while this one was closing, and the
    # remotes have already heard of it: then there is nothing to take back.
    if hub.tv is None:
        await hub.broadcast({"type": "tv", "connected": False})


# -------------------------------------------------------------------- a remote


async def serve_remote(svc: Services, ws: WebSocket) -> None:
    hub = svc.hub
    hub.remotes.add(ws)
    await hub.send(ws, {"type": "tv", "connected": hub.tv is not None})
    if hub.state is not None:
        await hub.send(ws, {"type": "state", **hub.state})
    # An empty queue is not sent: the page clears its copy whenever the connection drops.
    view = svc.playback.queue_view()
    if view:
        await hub.send(ws, {"type": "queue", "queue": view})
    try:
        while True:
            message = await ws.receive_json()
            if isinstance(message, dict) and message.get("type") == "command":
                error = await remote_command(svc, message)
                if error:
                    await hub.send(ws, {"type": "error", "message": error})
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("A remote connection failed")
        with suppress(Exception):
            await ws.close(code=1003)
    finally:
        hub.remotes.discard(ws)


async def remote_command(svc: Services, message: Dict[str, Any]) -> str:
    """Passes one command of a remote to the projector. Returns what to tell the remote if
    it could not be done, or an empty string."""
    hub = svc.hub
    action = message.get("action")
    if action not in REMOTE_ACTIONS:
        return ""
    if hub.tv is None:
        return "Проектор не на зв'язку"
    if action == "disconnect":
        await dismiss_tv(svc)
        return ""
    if action == "next":
        return "" if await svc.playback.play_next() else "Черга порожня"
    command: Dict[str, Any] = {"type": "command", "action": action}
    if action == "load":
        item = svc.videos.get(str(message.get("video_id")))
        if not item or item["status"] != "ready":
            return "Це відео недоступне"
        command.update(video_id=item["id"], name=item["name"])
    elif action in ("seek", "seek_by", "volume"):
        value = message.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            return ""
        try:
            command["value"] = float(value)
        except ValueError:
            return ""
    await hub.send(hub.tv, command)
    return ""
