"""The encoding settings page and its API."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from ..encoding import (
    AUDIO_BITRATES_KBPS, BITRATE_RANGE_KBPS, CRF_RANGE, FPS_CHOICES, HEIGHTS, SUGGESTED_KBPS,
    X264_PRESETS, Encoding, EncodingStore,
)
from .deps import AUTH, MEMBER, Svc, User, page

router = APIRouter()


class EncodingBody(BaseModel):
    max_height: int
    max_fps: int
    max_bitrate_kbps: int
    crf: int
    audio_bitrate_kbps: int
    x264_preset: str


def encoding_payload(encoding: EncodingStore) -> Dict[str, Any]:
    return {
        "encoding": asdict(encoding.get()),
        "defaults": asdict(encoding.defaults),
        "choices": {
            "max_height": sorted(HEIGHTS),
            "max_fps": list(FPS_CHOICES),
            "max_bitrate_kbps": list(BITRATE_RANGE_KBPS),
            "crf": list(CRF_RANGE),
            "audio_bitrate_kbps": list(AUDIO_BITRATES_KBPS),
            "x264_preset": list(X264_PRESETS),
        },
        "suggested_kbps": SUGGESTED_KBPS,
    }


@router.get("/settings", dependencies=AUTH)
async def settings_page(user: User) -> Response:
    if user["role"] == "device":
        return RedirectResponse("/", status_code=303)
    return page("settings.html")


@router.get("/api/settings", dependencies=MEMBER)
async def get_settings(svc: Svc) -> Dict[str, Any]:
    return encoding_payload(svc.encoding)


@router.put("/api/settings", dependencies=MEMBER)
async def put_settings(body: EncodingBody, svc: Svc) -> Dict[str, Any]:
    chosen = Encoding(**body.model_dump())
    problem = chosen.problem()
    if problem:
        raise HTTPException(400, problem)
    svc.encoding.set(chosen)
    return encoding_payload(svc.encoding)
