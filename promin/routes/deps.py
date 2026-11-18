"""What the route modules share: the app's services, who may call a route, pages, the cookie."""
from __future__ import annotations

import html
import re
from typing import Annotated, cast

from fastapi import Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from starlette.requests import HTTPConnection

from ..assets import asset_version
from ..auth import COOKIE_MAX_AGE, COOKIE_NAME, NotAuthenticated, Principal
from ..config import TEMPLATES_DIR
from ..services import Services


def services(conn: HTTPConnection) -> Services:
    return cast(Services, conn.app.state.services)


Svc = Annotated[Services, Depends(services)]


def require_auth(conn: HTTPConnection, svc: Svc) -> Principal:
    user = svc.auth.current_user(conn)
    if user is None:
        raise NotAuthenticated()
    return user


User = Annotated[Principal, Depends(require_auth)]


def require_member(user: User) -> Principal:
    """A person with a login, as opposed to a paired projector."""
    if user["role"] == "device":
        raise HTTPException(403, "Проектор може лише показувати відео")
    return user


def require_admin(user: User) -> Principal:
    if user["role"] != "admin":
        raise HTTPException(403, "Це може робити лише адміністратор")
    return user


# Who may call a route; every route names one of these, or is open on purpose.
# AUTH: everyone who is logged in, projectors included (pages, the list, video files);
# MEMBER: people with a login (adding and deleting videos, encoding settings);
# ADMIN: administrators only (users and paired projectors).
AUTH = [Depends(require_auth)]
MEMBER = [Depends(require_member)]
ADMIN = [Depends(require_admin)]


PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")


def page(name: str, status_code: int = 200, **values: str) -> HTMLResponse:
    """A page from templates/, with `{{name}}` replaced by the escaped value of `name`.
    `{{v}}` is always there: the version that the styles and scripts are named under."""
    values["v"] = asset_version()
    text = (TEMPLATES_DIR / name).read_text(encoding="utf-8")
    # One pass over the template: a placeholder inside an inserted value stays as text.
    text = PLACEHOLDER_RE.sub(
        lambda m: html.escape(values[m.group(1)]) if m.group(1) in values else m.group(0), text
    )
    return HTMLResponse(text, status_code)


def set_login_cookie(request: Request, response: Response, value: str) -> None:
    # Behind an HTTPS proxy (see FORWARDED_ALLOW_IPS in .env.example) the cookie is marked
    # Secure, so the browser never sends it over plain HTTP.
    response.set_cookie(
        COOKIE_NAME, value, max_age=COOKIE_MAX_AGE, httponly=True, samesite="lax",
        secure=request.url.scheme == "https",
    )


def client_address(request: Request) -> str:
    return request.client.host if request.client else "unknown"
