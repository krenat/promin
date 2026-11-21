"""Connecting a projector by a short link typed on it, instead of a QR code."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..devices import LINK_TTL
from ..shortener import ShortenerError
from .deps import MEMBER, Svc, User, page, set_login_cookie

log = logging.getLogger(__name__)

router = APIRouter()

# The one name the limit on new links is counted under.
ALL = "all"


class LinkToken(BaseModel):
    token: str


@router.post("/api/links", status_code=201, dependencies=MEMBER)
async def create_link(request: Request, user: User, svc: Svc) -> Dict[str, Any]:
    if svc.shortener is None:
        raise HTTPException(404, "Підключення за посиланням вимкнено")
    # A projector cannot open an address that only this machine understands.
    host = request.url.hostname or ""
    if host in ("localhost", "::1") or host.startswith("127."):
        raise HTTPException(
            400, "Відкрийте цю сторінку за адресою сервера, яку побачить і проектор, а не localhost."
        )
    # Counted for everyone together, and before the shortener is asked: it is this server
    # that the shortener counts, cancelled links included.
    if svc.links.full() or svc.link_limiter.blocked(ALL):
        raise HTTPException(429, "Забагато посилань. Спробуйте пізніше або скористайтесь QR-кодом.")
    svc.link_limiter.fail(ALL)
    token = svc.links.new_token()
    # The phone made this request, so its address is the one the projector can reach too.
    long_url = "%sc/%s" % (request.base_url, token)
    try:
        short = await asyncio.to_thread(svc.shortener.shorten, long_url, LINK_TTL)
    except ShortenerError as exc:
        log.warning("The shortener gave no link: %s", exc)
        raise HTTPException(
            502, "Не вдалося створити коротке посилання. Скористайтесь QR-кодом на сторінці /tv."
        )
    entry = svc.links.add(token, short, user["username"])
    return {"id": entry["id"], "short": short, "expires_in": LINK_TTL}


@router.delete("/api/links/{link_id}", status_code=204, dependencies=MEMBER)
async def cancel_link(link_id: str, user: User, svc: Svc) -> Response:
    """A link that was not opened yet: its maker or an administrator takes it back."""
    owner = None if user["role"] == "admin" else user["username"]
    if not svc.links.cancel(link_id, owner):
        raise HTTPException(404, "Посилання не знайдено")
    return Response(status_code=204)


# Where the short link leads. Open to everyone, and it spends nothing: shorteners and link
# previewers fetch an address themselves, and that must not use the link up. The page is
# the projector page; its script hands the token over.
@router.get("/c/{token}")
async def link_page(token: str) -> Response:
    response = page("tv.html")
    # The address holds the token: it must not travel to the sites the page loads from.
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@router.post("/api/links/redeem")
async def redeem_link(body: LinkToken, request: Request, svc: Svc) -> Response:
    # A browser that is logged in keeps its login: the cookie below would replace it.
    if svc.auth.current_user(request) is not None:
        raise HTTPException(409, "На цьому екрані вже виконано вхід")
    entry = svc.links.redeem(body.token)
    if entry is None:
        raise HTTPException(404, "Це посилання вже не діє")
    # The phone that shows the link stops showing it.
    await svc.hub.broadcast({"type": "link", "id": entry["id"]})
    response = JSONResponse({"ok": True})
    set_login_cookie(request, response, svc.devices.add("Проектор", entry["created_by"]))
    return response
