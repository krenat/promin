"""Paired projectors, and the ones that are showing a QR code to be paired."""
from __future__ import annotations

import secrets
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .accounts import SESSION_IDLE_TTL
from .crypto import hash_secret, same
from .jsonfile import read_json, records, write_json

# A paired projector is forgotten after as long without use as a user session.
DEVICE_IDLE_TTL = SESSION_IDLE_TTL

# Pairing a projector with a QR code: how long one lives and how many may wait at once.
PAIR_CODE_TTL = 10 * 60
PAIR_MAX_PENDING = 500
PAIR_STARTS_PER_WINDOW = 10


class DeviceStore:
    """Paired projectors. Only a hash of the device cookie secret is stored.

    A projector that has not been in touch for DEVICE_IDLE_TTL is forgotten: its cookie was
    lost (cleared browser, a new pairing) or it is no longer used.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._devices: Dict[str, Dict[str, Any]] = {}

    def load(self) -> None:
        with self._lock:
            check = records("name", "hash", "approved_by", "created")
            self._devices = read_json(self.path, check) or {}
            if self._purge():
                self._save()

    def _save(self) -> None:
        write_json(self.path, self._devices, private=True)

    @staticmethod
    def seen(device: Dict[str, Any]) -> float:
        """When the device was last in touch; one paired before this was recorded counts
        from its pairing."""
        return device.get("seen") or device["created"]

    def _purge(self) -> bool:
        cutoff = time.time() - DEVICE_IDLE_TTL
        stale = [key for key, d in self._devices.items() if self.seen(d) < cutoff]
        for key in stale:
            del self._devices[key]
        return bool(stale)

    def add(self, name: str, approved_by: str) -> str:
        """Creates a device and returns its cookie value."""
        device_id = secrets.token_hex(8)
        secret = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            self._purge()
            self._devices[device_id] = {
                "name": name,
                "hash": hash_secret(secret),
                "approved_by": approved_by,
                "created": now,
                "seen": now,
            }
            self._save()
        return "@%s.%s" % (device_id, secret)

    def touch(self, device_id: str) -> None:
        with self._lock:
            if device_id in self._devices:
                self._devices[device_id]["seen"] = time.time()
                self._save()

    def get(self, device_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            device = self._devices.get(device_id)
            return dict(device) if device else None

    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            if self._purge():
                self._save()
            items = [
                {
                    "id": key, "name": d["name"], "approved_by": d["approved_by"],
                    "created": d["created"], "seen": self.seen(d),
                }
                for key, d in self._devices.items()
            ]
        return sorted(items, key=lambda item: item["created"])

    def remove(self, device_id: str) -> bool:
        with self._lock:
            if device_id not in self._devices:
                return False
            del self._devices[device_id]
            self._save()
            return True


class Pairing:
    """Projectors that are showing a QR code right now. They live in memory only.

    Each has two secrets. The polling token stays with the projector; the approval key goes
    into the QR code on its screen. So someone who sees the screen can approve the projector
    (after logging in) but cannot collect the cookie meant for it.
    """

    def __init__(self) -> None:
        self.pending: Dict[str, Dict[str, Any]] = {}  # polling token → entry

    def purge(self) -> None:
        cutoff = time.monotonic() - PAIR_CODE_TTL
        for token in [t for t, entry in self.pending.items() if entry["created"] < cutoff]:
            del self.pending[token]

    def start(self) -> Optional[Dict[str, Any]]:
        self.purge()
        if len(self.pending) >= PAIR_MAX_PENDING:
            return None
        token = secrets.token_urlsafe(24)
        key = secrets.token_urlsafe(24)
        self.pending[token] = {"key": key, "created": time.monotonic(), "approved_by": None}
        return {"key": key, "token": token, "expires_in": PAIR_CODE_TTL}

    def by_key(self, key: str) -> Optional[Dict[str, Any]]:
        self.purge()
        for entry in self.pending.values():
            if entry["approved_by"] is None and same(entry["key"], key):
                return entry
        return None
