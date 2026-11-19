"""Bringing over the data of versions that kept it in JSON files, one file per subject."""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

from . import crypto
from .db import Database, sibling

log = logging.getLogger(__name__)

# The setting that says the JSON files were dealt with.
IMPORTED = "json_imported"
BAD_SHAPE = (ValueError, TypeError, KeyError, AttributeError)


Rows = List[Tuple[Any, ...]]


def read_file(path: Path, check: Callable[[Any], Any]) -> Any:
    """What a data file holds, as `check` returns it; None if nothing usable is there.

    Those versions wrote a file by writing `.tmp`, moving the old file to `.bak` and the
    new one into place, so after a power cut the newest readable copy may be any of the
    three. A file that cannot be used is moved aside as `.broken-<time>`.
    """
    for copy in (path, sibling(path, ".tmp"), sibling(path, ".bak")):
        if not copy.exists():
            continue
        try:
            data = check(json.loads(copy.read_text(encoding="utf-8")))
        except BAD_SHAPE:
            if copy == path:
                broken = sibling(path, ".broken-%d" % time.time())
                os.replace(path, broken)
                log.error("%s is damaged; kept as %s", path.name, broken.name)
            continue
        if copy != path:
            log.warning("%s could not be read; used %s", path.name, copy.name)
        return data
    return None


def records(data: Any, *keys: str) -> Dict[str, Dict[str, Any]]:
    """An object of objects that all have these keys, or the file counts as damaged."""
    if not isinstance(data, dict) or not all(isinstance(v, dict) for v in data.values()):
        raise TypeError("expected an object of objects")
    for name, value in data.items():
        missing = [key for key in keys if key not in value]
        if missing:
            raise KeyError("%s has no %s" % (name, ", ".join(missing)))
    return data


def number(value: Any) -> float:
    # bool is a number to Python.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("%r is not a number" % (value,))
    return float(value)


def optional(convert: Callable[[Any], Any], value: Any) -> Any:
    return None if value is None else convert(value)


# Each of these turns one parsed file into the rows of its table.


def video_rows(data: Any) -> Rows:
    return [
        (
            str(key), str(v["name"]), optional(int, v.get("size")), str(v["status"]),
            number(v["created"]), optional(str, v.get("source")),
            optional(number, v.get("duration")), int(bool(v.get("thumb"))),
            optional(int, v.get("thumb_v")), optional(str, v.get("error")),
            optional(str, v.get("folder")),
            json.dumps(v["info"], ensure_ascii=False) if isinstance(v.get("info"), dict) else None,
        )
        for key, v in records(data, "id", "name", "status", "created").items()
    ]


def folder_rows(data: Any) -> Rows:
    return [
        (str(key), str(f["name"]), number(f["created"]))
        for key, f in records(data, "name", "created").items()
    ]


def queue_rows(data: Any) -> Rows:
    if not isinstance(data, list):
        raise TypeError("expected a list")
    return [(str(e["entry"]), str(e["video_id"])) for e in data]


def user_rows(data: Any) -> Rows:
    return [
        (
            str(name), str(u["role"]), str(u["salt"]), str(u["hash"]),
            # Hashes written before the rounds were recorded took the old number.
            int(u.get("rounds") or crypto.LEGACY_PBKDF2_ROUNDS), number(u["created"]),
        )
        for name, u in records(data, "role", "salt", "hash", "created").items()
    ]


def session_rows(data: Any) -> Rows:
    return [
        (
            str(key), str(s["username"]), str(s["hash"]), str(s["fingerprint"]),
            number(s.get("created") or s["seen"]), number(s["seen"]),
        )
        for key, s in records(data, "username", "hash", "fingerprint", "seen").items()
    ]


def device_rows(data: Any) -> Rows:
    return [
        (
            str(key), str(d["name"]), str(d["hash"]), str(d["approved_by"]),
            # A projector paired before `seen` was recorded counts from its pairing.
            number(d["created"]), number(d.get("seen") or d["created"]),
        )
        for key, d in records(data, "name", "hash", "approved_by", "created").items()
    ]


def setting(key: str) -> Callable[[Any], Rows]:
    def rows(data: Any) -> Rows:
        if not isinstance(data, dict):
            raise TypeError("expected an object")
        return [(key, json.dumps(data, ensure_ascii=False))]

    return rows


# File → how to read it → where its rows go.
TABLES: Tuple[Tuple[str, Callable[[Any], Rows], str], ...] = (
    ("videos.json", video_rows,
     "videos (id, name, size, status, created, source, duration, thumb, thumb_v, error,"
     " folder, info) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"),
    ("folders.json", folder_rows, "folders (id, name, created) VALUES (?, ?, ?)"),
    ("queue.json", queue_rows, "queue (entry, video_id) VALUES (?, ?)"),
    ("users.json", user_rows,
     "users (username, role, salt, hash, rounds, created) VALUES (?, ?, ?, ?, ?, ?)"),
    ("sessions.json", session_rows,
     "sessions (id, username, hash, fingerprint, created, seen) VALUES (?, ?, ?, ?, ?, ?)"),
    ("devices.json", device_rows,
     "devices (id, name, hash, approved_by, created, seen) VALUES (?, ?, ?, ?, ?, ?)"),
    ("encoding.json", setting("encoding"), "settings (key, value) VALUES (?, ?)"),
    ("tvtest.json", setting("tvtest"), "settings (key, value) VALUES (?, ?)"),
)


def import_json(data_dir: Path, db: Database) -> None:
    """Fills a new database from the JSON files in the data directory.

    Done once per database, which remembers it: a start that was cut short before this
    finished does it again, and one after it never does. A file that cannot be used is left
    out, as it was before: a service that starts without its folders is better than one
    that does not start. What was read is stored together or not at all, and the files are
    then renamed to `.imported`: a copy that is never read again.
    """
    if db.get_setting(IMPORTED):
        return
    read = [
        (name, read_file(data_dir / name, to_rows), target) for name, to_rows, target in TABLES
    ]
    with db.transaction() as conn:
        for _, rows, target in read:
            if rows:
                conn.executemany("INSERT OR IGNORE INTO " + target, rows)
        db.set_setting(IMPORTED, True)
    counts = []
    for name, rows, _ in read:
        # The older copies go too: nothing may bring them back into a later database.
        for suffix in ("", ".tmp", ".bak"):
            path = sibling(data_dir / name, suffix)
            if path.exists():
                os.replace(path, sibling(path, ".imported"))
        if rows is not None:
            counts.append("%s (%d)" % (name, len(rows)))
    if counts:
        log.info("Brought over the data of an older version: %s", ", ".join(counts))
