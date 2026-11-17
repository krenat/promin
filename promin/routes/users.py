"""Users: the page and the API behind it, for administrators."""
from __future__ import annotations

import asyncio
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from ..accounts import BUILTIN_ADMIN, MIN_PASSWORD_LENGTH, ROLES, USERNAME_RE
from .deps import ADMIN, AUTH, Svc, User, page

router = APIRouter()


class NewUser(BaseModel):
    username: str
    password: str
    role: str = "viewer"


class NewPassword(BaseModel):
    password: str


def check_new_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(
            400, "Пароль має містити щонайменше %d символів" % MIN_PASSWORD_LENGTH
        )
    if len(password) > 200:
        raise HTTPException(400, "Пароль задовгий")


@router.get("/users", dependencies=AUTH)
async def users_page(user: User) -> Response:
    if user["role"] != "admin":
        return RedirectResponse("/", status_code=303)
    return page("users.html")


@router.get("/api/users", dependencies=ADMIN)
async def list_users(svc: Svc) -> Dict[str, Any]:
    builtin = {"username": BUILTIN_ADMIN, "role": "admin", "created": None, "builtin": True}
    return {"users": [builtin] + svc.users.list()}


@router.post("/api/users", status_code=201, dependencies=ADMIN)
async def create_user(body: NewUser, svc: Svc) -> Dict[str, Any]:
    username = body.username.strip().lower()
    if not USERNAME_RE.match(username):
        raise HTTPException(
            400, "Логін: від 2 до 32 символів, лише малі латинські літери, цифри, «-» та «_»"
        )
    if body.role not in ROLES:
        raise HTTPException(400, "Невідома роль")
    check_new_password(body.password)
    if username == BUILTIN_ADMIN or not await asyncio.to_thread(
        svc.users.add, username, body.password, body.role
    ):
        raise HTTPException(409, "Такий користувач уже є")
    return {"username": username, "role": body.role}


@router.post("/api/users/{username}/password", status_code=204, dependencies=ADMIN)
async def change_password(username: str, body: NewPassword, svc: Svc) -> Response:
    if username == BUILTIN_ADMIN:
        raise HTTPException(400, "Пароль вбудованого адміністратора задається в PROMIN_TOKEN")
    check_new_password(body.password)
    if not await asyncio.to_thread(svc.users.set_password, username, body.password):
        raise HTTPException(404, "Користувача не знайдено")
    await svc.drop_lost_connections()
    return Response(status_code=204)


@router.delete("/api/users/{username}", status_code=204, dependencies=ADMIN)
async def delete_user(username: str, user: User, svc: Svc) -> Response:
    if username == BUILTIN_ADMIN:
        raise HTTPException(400, "Вбудованого адміністратора видалити не можна")
    if username == user["username"]:
        raise HTTPException(400, "Не можна видалити себе")
    if not svc.users.remove(username):
        raise HTTPException(404, "Користувача не знайдено")
    await svc.drop_lost_connections()
    return Response(status_code=204)
