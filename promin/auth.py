"""Who is asking: the cookie, the login limit, and telling a user from a paired projector."""
from __future__ import annotations

import asyncio
import hmac
import time
from typing import Any, Dict, List, Optional

from starlette.requests import HTTPConnection

from .accounts import BUILTIN_ADMIN, SESSION_IDLE_TTL, SessionStore, UserStore
from . import crypto
from .crypto import hash_password, hash_secret, same
from .devices import DEVICE_IDLE_TTL, DeviceStore

COOKIE_NAME = "promin_session"
# The browser keeps the cookie as long as a session lives; every page load renews it
# (see /api/me).
COOKIE_MAX_AGE = SESSION_IDLE_TTL
SESSION_TOUCH_EVERY = 24 * 3600  # how often to record that a session was used

LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SECONDS = 15 * 60
LIMITER_MAX_ADDRESSES = 5000  # past this many, addresses with no recent failures are forgotten


class NotAuthenticated(Exception):
    pass


class LoginLimiter:
    """Limits failed login attempts from one address."""

    def __init__(self, max_failures: int = LOGIN_MAX_FAILURES, window: float = LOGIN_WINDOW_SECONDS) -> None:
        self.max_failures = max_failures
        self.window = window
        self._failures: Dict[str, List[float]] = {}

    def _recent(self, ip: str) -> List[float]:
        cutoff = time.monotonic() - self.window
        recent = [t for t in self._failures.get(ip, []) if t > cutoff]
        if recent:
            self._failures[ip] = recent
        else:
            self._failures.pop(ip, None)
        return recent

    def blocked(self, ip: str) -> bool:
        return len(self._recent(ip)) >= self.max_failures

    def fail(self, ip: str) -> float:
        """Records an attempt and returns its stamp, by which it can be forgiven."""
        stamp = time.monotonic()
        if len(self._failures) >= LIMITER_MAX_ADDRESSES:
            # Addresses that never return are only dropped here, so the table stays bounded.
            for known in list(self._failures):
                self._recent(known)
        self._failures.setdefault(ip, []).append(stamp)
        return stamp

    def forgive(self, ip: str, stamp: float) -> None:
        """Forgives one attempt (it turned out successful); other failures stay."""
        attempts = self._failures.get(ip)
        if attempts and stamp in attempts:
            attempts.remove(stamp)
            if not attempts:
                del self._failures[ip]


class Auth:
    """Tells who is asking by the cookie: a user with a login or a paired projector."""

    def __init__(
        self, token: str, users: UserStore, sessions: SessionStore, devices: DeviceStore
    ) -> None:
        self.token = token
        # Key for session fingerprints; the token itself never reaches a cookie or a file.
        self._secret = hmac.new(token.encode("utf-8"), b"promin-session", "sha256").digest()
        self.users = users
        self.sessions = sessions
        self.devices = devices

    def fingerprint(self, username: str) -> Optional[str]:
        """What a session depends on: a password change or deletion revokes it."""
        if username == BUILTIN_ADMIN:
            source = "admin:" + self.token
        else:
            user = self.users.get(username)
            if user is None:
                return None
            source = user["hash"]
        message = (username + "|" + source).encode("utf-8")
        return hmac.new(self._secret, message, "sha256").hexdigest()

    def current_user(self, conn: HTTPConnection) -> Optional[Dict[str, Any]]:
        raw = conn.cookies.get(COOKIE_NAME, "")
        if raw.startswith("@"):  # a paired projector
            device_id, _, secret = raw[1:].partition(".")
            device = self.devices.get(device_id)
            digest = hash_secret(secret)
            if device is None or not same(digest, device["hash"]):
                return None
            idle = time.time() - device["seen"]
            if idle > DEVICE_IDLE_TTL:
                self.devices.remove(device_id)
                return None
            if idle > SESSION_TOUCH_EVERY:
                self.devices.touch(device_id)
            return {"username": device["name"], "role": "device", "device_id": device_id}
        session_id, _, secret = raw.partition(".")
        session = self.sessions.get(session_id) if session_id and secret else None
        digest = hash_secret(secret)
        if session is None or not same(digest, session["hash"]):
            return None
        username = session["username"]
        current = self.fingerprint(username)
        idle = time.time() - session["seen"]
        if current is None or not same(current, session["fingerprint"]) or idle > SESSION_IDLE_TTL:
            self.sessions.remove(session_id)
            return None
        if idle > SESSION_TOUCH_EVERY:
            self.sessions.touch(session_id)
        role = "admin" if username == BUILTIN_ADMIN else (self.users.get(username) or {}).get("role")
        return {"username": username, "role": role}

    async def check_password(self, username: str, password: str) -> bool:
        if username == BUILTIN_ADMIN:
            return same(password, self.token)
        user = self.users.get(username)
        # Hash for an unknown name too, so the response does not reveal whether it exists.
        salt = bytes.fromhex(user["salt"]) if user else b"\0" * 16
        rounds = user["rounds"] if user else crypto.PBKDF2_ROUNDS
        digest = await asyncio.to_thread(hash_password, password, salt, rounds)
        if user is None or not same(digest, user["hash"]):
            return False
        if rounds < crypto.PBKDF2_ROUNDS:
            await asyncio.to_thread(self._strengthen, username, password, user["hash"])
        return True

    def _strengthen(self, username: str, password: str, old_hash: str) -> None:
        """Stores a password that was just checked under today's number of rounds. Only a
        login can do this: the password itself is never kept. Sessions that are open on
        other phones stay open."""
        before = self.fingerprint(username)
        if not self.users.set_password(username, password, only_if_hash=old_hash):
            return
        after = self.fingerprint(username)
        if before and after:
            self.sessions.refingerprint(username, before, after)

    def login(self, username: str) -> str:
        """Starts a session for a user whose password was checked; returns the cookie value."""
        return self.sessions.add(username, self.fingerprint(username) or "")

    def logout(self, raw: str) -> bool:
        """Drops the session of this cookie value. True if there was one."""
        if not raw or raw.startswith("@"):
            return False
        session_id, _, secret = raw.partition(".")
        session = self.sessions.get(session_id)
        digest = hash_secret(secret)
        if session is None or not same(digest, session["hash"]):
            return False
        return self.sessions.remove(session_id)
