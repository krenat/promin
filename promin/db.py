"""The data file: one SQLite database for everything that is not a video file."""
from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import threading
import time
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, Iterator, List, Optional, Sequence

log = logging.getLogger(__name__)

DB_NAME = "promin.db"

# Raise SCHEMA_VERSION and add a step to Database._migrate when the tables change.
SCHEMA_VERSION = 1
SCHEMA = """
CREATE TABLE videos (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    size INTEGER,
    status TEXT NOT NULL,
    created REAL NOT NULL,
    source TEXT,
    duration REAL,
    thumb INTEGER NOT NULL DEFAULT 0,
    thumb_v INTEGER,
    error TEXT,
    folder TEXT,
    info TEXT
);
CREATE TABLE folders (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created REAL NOT NULL
);
CREATE TABLE queue (
    position INTEGER PRIMARY KEY,
    entry TEXT NOT NULL UNIQUE,
    video_id TEXT NOT NULL
);
CREATE TABLE users (
    username TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    salt TEXT NOT NULL,
    hash TEXT NOT NULL,
    rounds INTEGER NOT NULL,
    created REAL NOT NULL
);
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    hash TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    created REAL NOT NULL,
    seen REAL NOT NULL
);
CREATE TABLE devices (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    hash TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    created REAL NOT NULL,
    seen REAL NOT NULL
);
CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def sibling(path: Path, suffix: str) -> Path:
    return path.with_name(path.name + suffix)


class Damaged(sqlite3.DatabaseError):
    """The file is not a database that can be read."""


class Database:
    """One connection shared by the whole app, used by one thread at a time.

    A change is a transaction: several stores can take part in one (see `transaction`), and
    a power cut leaves either all of it or none. The journal is a write-ahead log that is
    not forced to the disk on every change, so a write does not hold up the event loop; a
    power cut may lose the last moments, never the file.

    Every start keeps a copy of the database as `.bak`. A file that cannot be read is moved
    aside as `.broken-<time>` and the copy is used instead.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._depth = 0

    # ---------------------------------------------------------------- open, close

    def open(self) -> None:
        """Opens the database, creating or repairing it as needed."""
        with self._lock:
            try:
                self._conn = self._connect()
            except Damaged:
                self._conn = self._repair()
            self._keep_copy()

    def _repair(self) -> sqlite3.Connection:
        """Sets a database that cannot be read aside and opens the copy of the last start
        in its place, or an empty one if the copy is no better."""
        self._set_aside(self.path)
        backup = sibling(self.path, ".bak")
        if backup.exists():
            shutil.copyfile(backup, self.path)
            try:
                conn = self._connect()
            except Damaged:
                # What was copied into place is dropped; the copy itself goes aside.
                for suffix in ("", "-wal", "-shm"):
                    sibling(self.path, suffix).unlink(missing_ok=True)
                self._set_aside(backup)
            else:
                log.warning("Using the copy %s kept at the last start", backup.name)
                return conn
        return self._connect()

    def _connect(self) -> sqlite3.Connection:
        # The file is private before anything is written: password hashes are in here, and
        # SQLite gives its journal the permissions the database has when the journal is made.
        os.close(os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600))
        with suppress(OSError):
            os.chmod(self.path, 0o600)
        # isolation_level=None: transactions begin and end where this class says, not
        # where the sqlite3 module guesses.
        conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            try:
                if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise Damaged("quick_check failed")
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                self._migrate(conn)
            except sqlite3.OperationalError:
                # Locked by another process, a full disk, a failing read: trouble around
                # the file, not in it. The service does not start, and the file stays put.
                raise
            except sqlite3.DatabaseError as exc:
                raise Damaged(str(exc)) from exc
        except BaseException:
            conn.close()
            raise
        return conn

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                "The data was written by a newer Promin (schema %d, this one knows %d)"
                % (version, SCHEMA_VERSION)
            )
        if version < 1:
            conn.executescript("BEGIN;" + SCHEMA + "PRAGMA user_version=1; COMMIT;")

    @staticmethod
    def _set_aside(path: Path) -> None:
        """Moves a file that cannot be read out of the way, for a person to look at, under
        a name nothing else has."""
        stamp, n = ".broken-%d" % time.time(), 1
        while sibling(path, stamp).exists():
            n += 1
            stamp = ".broken-%d-%d" % (time.time(), n)
        for suffix in ("", "-wal", "-shm"):
            part = sibling(path, suffix)
            if part.exists():
                os.replace(part, sibling(path, stamp + suffix))
        log.error("%s is damaged; kept as %s%s", path.name, path.name, stamp)

    def _keep_copy(self) -> None:
        assert self._conn is not None
        tmp, backup = sibling(self.path, ".bak.tmp"), sibling(self.path, ".bak")
        try:
            tmp.unlink(missing_ok=True)
            target = sqlite3.connect(str(tmp))
            try:
                self._conn.backup(target)
            finally:
                target.close()
            os.chmod(tmp, 0o600)
            os.replace(tmp, backup)
        except (OSError, sqlite3.Error):
            log.exception("Could not keep a copy of %s", self.path.name)

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # ------------------------------------------------------------------ the work

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("The database is not open")
        return self._conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Everything done inside is stored together or not at all. A transaction opened
        inside another one joins it, so an operation that spans several stores wraps their
        own calls in one of these."""
        with self._lock:
            conn = self.conn
            if self._depth == 0:
                conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield conn
            except BaseException:
                self._depth -= 1
                if self._depth == 0:
                    conn.execute("ROLLBACK")
                raise
            else:
                self._depth -= 1
                if self._depth == 0:
                    conn.execute("COMMIT")

    @contextmanager
    def reading(self) -> Iterator[sqlite3.Connection]:
        """The connection for a few reads that no other thread comes between."""
        with self._lock:
            yield self.conn

    def all(self, sql: str, params: Sequence[Any] = ()) -> List[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: Sequence[Any] = ()) -> Optional[sqlite3.Row]:
        with self._lock:
            row: Optional[sqlite3.Row] = self.conn.execute(sql, params).fetchone()
            return row

    def run(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Makes one change and returns how many rows it touched."""
        with self.transaction() as conn:
            return conn.execute(sql, params).rowcount

    # -------------------------------------------------- single values, by name

    def get_setting(self, key: str) -> Any:
        """A value stored by set_setting; None if there is none or it cannot be read."""
        row = self.one("SELECT value FROM settings WHERE key = ?", (key,))
        if row is None:
            return None
        try:
            return json.loads(row["value"])
        except ValueError:
            return None

    def set_setting(self, key: str, value: Any) -> None:
        self.run(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, json.dumps(value, ensure_ascii=False)),
        )
