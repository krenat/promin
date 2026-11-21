from __future__ import annotations

import re
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, quote

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from ..auth import COOKIE_NAME
from .deps import AUTH, Svc, User, client_address, page, set_login_cookie

LOGIN_MAX_BODY = 4096
# Where login may redirect to.
LOGIN_NEXT = ("/", "/tv")
# The address inside the QR code: login may return to it, with a well-formed key only.
PAIR_NEXT_RE = re.compile(r"/pair\?t=[A-Za-z0-9_-]{16,64}")

router = APIRouter()


def safe_next(value: Optional[str]) -> str:
    if value in LOGIN_NEXT or (value and PAIR_NEXT_RE.fullmatch(value)):
        return value
    return "/"


async def not_authenticated(request: Request, exc: Exception) -> Response:
    """What someone without a login gets: pages lead to the login form, the API says 401."""
    if request.method == "GET" and not request.url.path.startswith("/api/"):
        path = request.url.path
        if path == "/pair":  # a scanned QR code: come back to it after the login
            path += "?t=" + request.query_params.get("t", "")
        back = safe_next(path)
        target = "/login?next=" + quote(back, safe="") if back != "/" else "/login"
        return RedirectResponse(target, status_code=303)
    return JSONResponse({"detail": "Потрібен вхід"}, status_code=401)


def login_page(error: str = "", status_code: int = 200, next_path: str = "/") -> Response:
    return page("login.html", status_code, error=error, next=next_path)


@router.get("/login")
async def login_form(request: Request, svc: Svc) -> Response:
    next_path = safe_next(request.query_params.get("next"))
    if svc.auth.current_user(request):
        return RedirectResponse(next_path, status_code=303)
    return login_page(next_path=next_path)


@router.post("/login")
async def login(request: Request, svc: Svc) -> Response:
    ip = client_address(request)
    if svc.limiter.blocked(ip):
        return login_page("Забагато спроб. Спробуйте знову за 15 хвилин.", 429)
    # Count the attempt before checking the password: there is no await between the limit
    # check and the record, so parallel requests cannot bypass the limit.
    attempt = svc.limiter.fail(ip)
    # Read the body as a stream and stop at the limit: Content-Length may be missing.
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > LOGIN_MAX_BODY:
            return login_page("Забагато даних.", 413)
    form = parse_qs(body.decode("utf-8", errors="replace"))
    username = (form.get("username") or [""])[0].strip().lower()
    password = (form.get("password") or [""])[0]
    next_path = safe_next((form.get("next") or [""])[0])
    if not await svc.auth.check_password(username, password):
        return login_page("Неправильний логін або пароль.", 401, next_path)
    # A successful login forgives only its own attempt: earlier failures stay.
    svc.limiter.forgive(ip, attempt)
    response = RedirectResponse(next_path, status_code=303)
    set_login_cookie(request, response, svc.auth.login(username))
    return response


@router.post("/logout")
async def logout(request: Request, svc: Svc) -> Response:
    # Drop the session on the server too: a stolen cookie stops working after logout.
    if svc.auth.logout(request.cookies.get(COOKIE_NAME, "")):
        await svc.drop_lost_connections()
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE_NAME)
    return response


@router.get("/api/me", dependencies=AUTH)
async def me(request: Request, response: Response, user: User, svc: Svc) -> Dict[str, Any]:
    # Every page asks this as it opens, so the cookie is renewed here: the browser keeps
    # it for COOKIE_MAX_AGE after the last visit, not after the login or the pairing.
    set_login_cookie(request, response, request.cookies[COOKIE_NAME])
    # `links`: whether the phone page offers to connect a projector by a short link.
    return dict(user, links=svc.shortener is not None)
