"""Paired projectors, the ones that are showing a QR code to be paired, and the links that
connect one without a QR code."""
from __future__ import annotations

import secrets
import sqlite3
import time
from typing import Dict, List, Optional, TypedDict

from .accounts import SESSION_IDLE_TTL
from .crypto import hash_secret, same
from .db import Database

# A paired projector is forgotten after as long without use as a user session.
DEVICE_IDLE_TTL = SESSION_IDLE_TTL

# Pairing a projector with a QR code: how long one lives and how many may wait at once.
PAIR_CODE_TTL = 10 * 60
PAIR_MAX_PENDING = 500
PAIR_STARTS_PER_WINDOW = 10

# Connecting a projector by a short link: how long a link waits to be opened, and how many
# may wait at once.
LINK_TTL = 10 * 60
LINK_MAX_PENDING = 20


class Device(TypedDict):
    id: str
    name: str
    hash: str
    approved_by: str
    created: float
    seen: float


class DeviceSummary(TypedDict):
    id: str
    name: str
    approved_by: str
    created: float
    seen: float


class DeviceStore:
    """Paired projectors. Only a hash of the device cookie secret is stored.

    A projector that has not been in touch for DEVICE_IDLE_TTL is forgotten: its cookie was
    lost (cleared browser, a new pairing) or it is no longer used.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    def load(self) -> None:
        self._purge()

    def _purge(self) -> None:
        self.db.run("DELETE FROM devices WHERE seen < ?", (time.time() - DEVICE_IDLE_TTL,))

    def add(self, name: str, approved_by: str) -> str:
        """Creates a device and returns its cookie value."""
        device_id = secrets.token_hex(8)
        secret = secrets.token_urlsafe(32)
        now = time.time()
        with self.db.transaction() as conn:
            self._purge()
            conn.execute(
                "INSERT INTO devices (id, name, hash, approved_by, created, seen)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (device_id, name, hash_secret(secret), approved_by, now, now),
            )
        return "@%s.%s" % (device_id, secret)

    def touch(self, device_id: str) -> None:
        self.db.run("UPDATE devices SET seen = ? WHERE id = ?", (time.time(), device_id))

    @staticmethod
    def _device(row: sqlite3.Row) -> Device:
        return Device(
            id=row["id"], name=row["name"], hash=row["hash"], approved_by=row["approved_by"],
            created=row["created"], seen=row["seen"],
        )

    def get(self, device_id: str) -> Optional[Device]:
        row = self.db.one("SELECT * FROM devices WHERE id = ?", (device_id,))
        return self._device(row) if row else None

    def list(self) -> List[DeviceSummary]:
        with self.db.transaction() as conn:
            self._purge()
            rows = conn.execute("SELECT * FROM devices ORDER BY created").fetchall()
        return [
            DeviceSummary(
                id=row["id"], name=row["name"], approved_by=row["approved_by"],
                created=row["created"], seen=row["seen"],
            )
            for row in rows
        ]

    def remove(self, device_id: str) -> bool:
        return self.db.run("DELETE FROM devices WHERE id = ?", (device_id,)) > 0


class PendingPairing(TypedDict):
    key: str  # the approval key, shown in the QR code
    created: float
    approved_by: Optional[str]  # who confirmed it on a phone; None until someone does


class PairingStart(TypedDict):
    key: str
    token: str  # the polling token, which stays with the projector
    expires_in: int


class Pairing:
    """Projectors that are showing a QR code right now. They live in memory only.

    Each has two secrets. The polling token stays with the projector; the approval key goes
    into the QR code on its screen. So someone who sees the screen can approve the projector
    (after logging in) but cannot collect the cookie meant for it.
    """

    def __init__(self) -> None:
        self.pending: Dict[str, PendingPairing] = {}  # polling token → entry

    def purge(self) -> None:
        cutoff = time.monotonic() - PAIR_CODE_TTL
        for token in [t for t, entry in self.pending.items() if entry["created"] < cutoff]:
            del self.pending[token]

    def start(self) -> Optional[PairingStart]:
        self.purge()
        if len(self.pending) >= PAIR_MAX_PENDING:
            return None
        token = secrets.token_urlsafe(24)
        key = secrets.token_urlsafe(24)
        self.pending[token] = {"key": key, "created": time.monotonic(), "approved_by": None}
        return {"key": key, "token": token, "expires_in": PAIR_CODE_TTL}

    def by_key(self, key: str) -> Optional[PendingPairing]:
        self.purge()
        for entry in self.pending.values():
            if entry["approved_by"] is None and same(entry["key"], key):
                return entry
        return None


class PendingLink(TypedDict):
    id: str
    hash: str  # of the token in the long address the short one leads to
    short: str  # the short address, as it is typed on the projector
    created_by: str
    created: float


class Links:
    """Short links that are waiting to be opened on a projector. They live in memory only.

    A link works once: the screen that opens it becomes a paired projector, approved by
    whoever made the link, and the link is gone. So the address left in that screen's
    history opens nothing, whether or not the projector is disconnected later.
    """

    def __init__(self) -> None:
        self.pending: Dict[str, PendingLink] = {}  # id → entry

    def purge(self) -> None:
        cutoff = time.monotonic() - LINK_TTL
        for link_id in [i for i, entry in self.pending.items() if entry["created"] < cutoff]:
            del self.pending[link_id]

    def full(self) -> bool:
        self.purge()
        return len(self.pending) >= LINK_MAX_PENDING

    @staticmethod
    def new_token() -> str:
        return secrets.token_urlsafe(24)

    def add(self, token: str, short: str, created_by: str) -> PendingLink:
        entry = PendingLink(
            id=secrets.token_hex(8), hash=hash_secret(token), short=short,
            created_by=created_by, created=time.monotonic(),
        )
        self.pending[entry["id"]] = entry
        return entry

    def redeem(self, token: str) -> Optional[PendingLink]:
        """Spends the link of this token and returns it; None if there is none to spend."""
        self.purge()
        digest = hash_secret(token)
        for link_id, entry in self.pending.items():
            if same(entry["hash"], digest):
                return self.pending.pop(link_id)
        return None

    def cancel(self, link_id: str, username: Optional[str]) -> bool:
        """Drops a link that was not opened. With a username, only that user's own."""
        entry = self.pending.get(link_id)
        if entry is None or (username is not None and entry["created_by"] != username):
            return False
        del self.pending[link_id]
        return True
