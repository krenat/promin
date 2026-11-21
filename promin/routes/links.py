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


class LinkToken(BaseModel):
    token: str


@router.post("/api/links", status_code=201, dependencies=MEMBER)
async def create_link(request: Request, user: User, svc: Svc) -> Dict[str, Any]:
    if svc.shortener is None:
        raise HTTPException(404, "Підключення за посиланням вимкнено")
    if svc.links.full():
        raise HTTPException(429, "Забагато посилань чекає. Скасуйте зайві або зачекайте.")
    token = svc.links.new_token()
    # The phone made this request, so its address is the one the projector can reach too.
    long_url = "%sc/%s" % (request.base_url, token)
    try:
        short = await asyncio.to_thread(svc.shortener.shorten, long_url)
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
    return page("tv.html")


@router.post("/api/links/redeem")
async def redeem_link(body: LinkToken, request: Request, svc: Svc) -> Response:
    entry = svc.links.redeem(body.token)
    if entry is None:
        raise HTTPException(404, "Це посилання вже не діє")
    response = JSONResponse({"ok": True})
    set_login_cookie(request, response, svc.devices.add("Проектор", entry["created_by"]))
    return response
