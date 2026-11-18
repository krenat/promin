"""People with a login: users and their sessions, each in a data file."""
from __future__ import annotations

import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import crypto
from .crypto import hash_password, hash_secret
from .jsonfile import read_json, records, write_json

# Built-in administrator: its password is PROMIN_TOKEN and is never stored in a file.
BUILTIN_ADMIN = "admin"
ROLES = ("admin", "viewer")
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")
MIN_PASSWORD_LENGTH = 8

# A user session ends after this long without use.
SESSION_IDLE_TTL = 30 * 24 * 3600


class UserStore:
    """Users in a JSON file; passwords are stored only as hashes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._users: Dict[str, Dict[str, Any]] = {}

    def load(self) -> None:
        with self._lock:
            self._users = read_json(self.path, records("role", "salt", "hash", "created")) or {}

    def _save(self) -> None:
        write_json(self.path, self._users, private=True)

    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                {"username": name, "role": user["role"], "created": user["created"]}
                for name, user in sorted(self._users.items())
            ]

    def get(self, username: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            user = self._users.get(username)
            return dict(user) if user else None

    @staticmethod
    def rounds(user: Dict[str, Any]) -> int:
        """How many PBKDF2 rounds the hash of this user took; one stored before this was
        recorded took the old number."""
        return int(user.get("rounds") or crypto.LEGACY_PBKDF2_ROUNDS)

    @staticmethod
    def _hashed(password: str) -> Dict[str, Any]:
        salt = secrets.token_bytes(16)
        rounds = crypto.PBKDF2_ROUNDS
        return {"salt": salt.hex(), "hash": hash_password(password, salt, rounds), "rounds": rounds}

    def add(self, username: str, password: str, role: str) -> bool:
        hashed = self._hashed(password)
        with self._lock:
            if username in self._users:
                return False
            self._users[username] = dict(hashed, role=role, created=time.time())
            self._save()
            return True

    def set_password(self, username: str, password: str, only_if_hash: Optional[str] = None) -> bool:
        """Stores a new hash of the password. With `only_if_hash`, only while the user still
        has that hash: a password changed in the meantime is left alone."""
        hashed = self._hashed(password)
        with self._lock:
            user = self._users.get(username)
            if user is None or (only_if_hash is not None and user["hash"] != only_if_hash):
                return False
            user.update(hashed)
            self._save()
            return True

    def remove(self, username: str) -> bool:
        with self._lock:
            if username not in self._users:
                return False
            del self._users[username]
            self._save()
            return True


class SessionStore:
    """User sessions. Only a hash of the cookie secret is stored."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._sessions: Dict[str, Dict[str, Any]] = {}

    def load(self) -> None:
        with self._lock:
            check = records("username", "hash", "fingerprint", "seen")
            self._sessions = read_json(self.path, check) or {}

    def _save(self) -> None:
        write_json(self.path, self._sessions, private=True)

    def add(self, username: str, fingerprint: str) -> str:
        """Creates a session and returns its cookie value. Also purges expired ones."""
        session_id = secrets.token_hex(8)
        secret = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            for key in [k for k, v in self._sessions.items() if v["seen"] < now - SESSION_IDLE_TTL]:
                del self._sessions[key]
            self._sessions[session_id] = {
                "username": username,
                "hash": hash_secret(secret),
                "fingerprint": fingerprint,
                "created": now,
                "seen": now,
            }
            self._save()
        return "%s.%s" % (session_id, secret)

    def get(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            session = self._sessions.get(session_id)
            return dict(session) if session else None

    def touch(self, session_id: str) -> None:
        with self._lock:
            if session_id in self._sessions:
                self._sessions[session_id]["seen"] = time.time()
                self._save()

    def refingerprint(self, username: str, old: str, new: str) -> None:
        """Moves the sessions of a user from one fingerprint to another, so that storing
        the same password under a stronger hash does not log anyone out."""
        with self._lock:
            mine = [
                s for s in self._sessions.values()
                if s["username"] == username and s["fingerprint"] == old
            ]
            for session in mine:
                session["fingerprint"] = new
            if mine:
                self._save()

    def remove(self, session_id: str) -> bool:
        with self._lock:
            if session_id not in self._sessions:
                return False
            del self._sessions[session_id]
            self._save()
            return True
