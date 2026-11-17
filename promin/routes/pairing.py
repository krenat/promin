"""Pairing a projector with a QR code, and the list of paired projectors."""
from __future__ import annotations

import io
from typing import Any, Dict

import segno
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from .deps import ADMIN, AUTH, Svc, User, client_address, page, set_login_cookie

router = APIRouter()


class PairToken(BaseModel):
    token: str


class PairKey(BaseModel):
    key: str


@router.post("/api/pair/start")
async def pair_start(request: Request, svc: Svc) -> Dict[str, Any]:
    ip = client_address(request)
    if svc.pair_start_limiter.blocked(ip):
        raise HTTPException(429, "Забагато спроб. Спробуйте пізніше.")
    svc.pair_start_limiter.fail(ip)
    started = svc.pairing.start()
    if started is None:
        raise HTTPException(429, "Забагато пристроїв чекає на підключення.")
    # The phone opens this address from its camera, so it is the one the projector used.
    url = "%spair?t=%s" % (request.base_url, started.pop("key"))
    picture = io.BytesIO()
    segno.make(url, error="m").save(
        picture, kind="svg", xmldecl=False, svgns=True, omitsize=True,
        scale=10, border=4, dark="#000", light="#fff",
    )
    return dict(started, url=url, qr=picture.getvalue().decode("utf-8"))


@router.post("/api/pair/status")
async def pair_status(body: PairToken, request: Request, svc: Svc) -> Response:
    pairing = svc.pairing
    pairing.purge()
    entry = pairing.pending.get(body.token)
    if entry is None:
        return JSONResponse({"status": "expired"})
    if entry["approved_by"] is None:
        return JSONResponse({"status": "pending"})
    # Approved on the phone: the QR code is spent. The device is recorded only now, as
    # the projector collects its cookie, so an approval nobody collects leaves no device.
    del pairing.pending[body.token]
    response = JSONResponse({"status": "approved"})
    set_login_cookie(request, response, svc.devices.add("Проектор", entry["approved_by"]))
    return response


@router.post("/api/pair/approve", dependencies=AUTH)
async def pair_approve(body: PairKey, user: User, svc: Svc) -> Dict[str, Any]:
    if user["role"] == "device":
        raise HTTPException(403, "Підключати проектор може лише користувач")
    entry = svc.pairing.by_key(body.key)
    if entry is None:
        raise HTTPException(404, "Цей QR-код уже не діє. Зчитайте новий з екрана проектора.")
    entry["approved_by"] = user["username"]
    return {"ok": True}


@router.get("/pair", dependencies=AUTH)
async def pair_page(user: User) -> Response:
    """Where the QR code leads: the phone asks before it lets the projector in."""
    if user["role"] == "device":
        return RedirectResponse("/", status_code=303)
    return page("pair.html")


@router.get("/api/devices", dependencies=ADMIN)
async def list_devices(svc: Svc) -> Dict[str, Any]:
    return {"devices": svc.devices.list()}


@router.delete("/api/devices/{device_id}", status_code=204, dependencies=ADMIN)
async def delete_device(device_id: str, svc: Svc) -> Response:
    if not svc.devices.remove(device_id):
        raise HTTPException(404, "Пристрій не знайдено")
    # If this projector is connected right now, playback on it stops at once.
    await svc.drop_lost_connections()
    return Response(status_code=204)
