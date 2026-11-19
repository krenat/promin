"""People with a login: users and their sessions."""
from __future__ import annotations

import re
import secrets
import time
from typing import Any, List, Literal, Optional, Tuple, TypedDict

from . import crypto
from .crypto import hash_password, hash_secret
from .db import Database

# Built-in administrator: its password is PROMIN_TOKEN and is never stored.
BUILTIN_ADMIN = "admin"
Role = Literal["admin", "viewer"]
ROLES = ("admin", "viewer")
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")
MIN_PASSWORD_LENGTH = 8

# A user session ends after this long without use.
SESSION_IDLE_TTL = 30 * 24 * 3600


class UserRecord(TypedDict):
    """A user as stored: the password only as a hash, with what it takes to check it."""

    role: Role
    salt: str
    hash: str
    rounds: int
    created: float


class UserSummary(TypedDict):
    username: str
    role: Role
    created: float


class Session(TypedDict):
    username: str
    hash: str
    fingerprint: str
    created: float
    seen: float


class UserStore:
    """Users; passwords are stored only as hashes."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def list(self) -> List[UserSummary]:
        return [
            UserSummary(username=row["username"], role=row["role"], created=row["created"])
            for row in self.db.all("SELECT username, role, created FROM users ORDER BY username")
        ]

    def get(self, username: str) -> Optional[UserRecord]:
        row = self.db.one("SELECT * FROM users WHERE username = ?", (username,))
        if row is None:
            return None
        return UserRecord(
            role=row["role"], salt=row["salt"], hash=row["hash"], rounds=row["rounds"],
            created=row["created"],
        )

    def add(self, username: str, password: str, role: Role) -> bool:
        """False if the name is taken."""
        salt, digest, rounds = self.hashed(password)
        return self.db.run(
            "INSERT OR IGNORE INTO users (username, role, salt, hash, rounds, created)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (username, role, salt, digest, rounds, time.time()),
        ) > 0

    @staticmethod
    def hashed(password: str) -> Tuple[str, str, int]:
        """A password as it is stored: the salt, the hash and its rounds. Slow on purpose,
        so it is worked out before the database is touched, never inside a transaction."""
        salt = secrets.token_bytes(16)
        rounds = crypto.PBKDF2_ROUNDS
        return salt.hex(), hash_password(password, salt, rounds), rounds

    def set_password(self, username: str, password: str, only_if_hash: Optional[str] = None) -> bool:
        """Stores a new hash of the password. With `only_if_hash`, only while the user still
        has that hash: a password changed in the meantime is left alone."""
        return self.set_hash(username, self.hashed(password), only_if_hash)

    def set_hash(
        self, username: str, hashed: Tuple[str, str, int], only_if_hash: Optional[str] = None
    ) -> bool:
        salt, digest, rounds = hashed
        sql = "UPDATE users SET salt = ?, hash = ?, rounds = ? WHERE username = ?"
        params: List[Any] = [salt, digest, rounds, username]
        if only_if_hash is not None:
            sql += " AND hash = ?"
            params.append(only_if_hash)
        return self.db.run(sql, params) > 0

    def remove(self, username: str) -> bool:
        return self.db.run("DELETE FROM users WHERE username = ?", (username,)) > 0


class SessionStore:
    """User sessions. Only a hash of the cookie secret is stored."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def add(self, username: str, fingerprint: str) -> str:
        """Creates a session and returns its cookie value. Also purges expired ones."""
        session_id = secrets.token_hex(8)
        secret = secrets.token_urlsafe(32)
        now = time.time()
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM sessions WHERE seen < ?", (now - SESSION_IDLE_TTL,))
            conn.execute(
                "INSERT INTO sessions (id, username, hash, fingerprint, created, seen)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, username, hash_secret(secret), fingerprint, now, now),
            )
        return "%s.%s" % (session_id, secret)

    def get(self, session_id: str) -> Optional[Session]:
        row = self.db.one("SELECT * FROM sessions WHERE id = ?", (session_id,))
        if row is None:
            return None
        return Session(
            username=row["username"], hash=row["hash"], fingerprint=row["fingerprint"],
            created=row["created"], seen=row["seen"],
        )

    def touch(self, session_id: str) -> None:
        self.db.run("UPDATE sessions SET seen = ? WHERE id = ?", (time.time(), session_id))

    def refingerprint(self, username: str, old: str, new: str) -> None:
        """Moves the sessions of a user from one fingerprint to another, so that storing
        the same password under a stronger hash does not log anyone out."""
        self.db.run(
            "UPDATE sessions SET fingerprint = ? WHERE username = ? AND fingerprint = ?",
            (new, username, old),
        )

    def remove(self, session_id: str) -> bool:
        return self.db.run("DELETE FROM sessions WHERE id = ?", (session_id,)) > 0
