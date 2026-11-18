"""The data file: one SQLite database for everything that is not a video file."""
from __future__ import annotations

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

    def open(self) -> bool:
        """Opens the database, creating or repairing it as needed. True if it is new and
        empty: there was no data file before this start."""
        with self._lock:
            fresh = not self.path.exists()
            try:
                self._conn = self._connect()
            except sqlite3.DatabaseError:
                self._set_aside()
                backup = sibling(self.path, ".bak")
                if backup.exists():
                    shutil.copyfile(backup, self.path)
                try:
                    self._conn = self._connect()
                    if backup.exists():
                        log.warning("Using the copy %s kept at the last start", backup.name)
                except sqlite3.DatabaseError:
                    self._set_aside()
                    self._conn = self._connect()
            with suppress(OSError):
                os.chmod(self.path, 0o600)  # password hashes are in here
            self._keep_copy()
            return fresh

    def _connect(self) -> sqlite3.Connection:
        # isolation_level=None: transactions begin and end where this class says, not
        # where the sqlite3 module guesses.
        conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("quick_check failed")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._migrate(conn)
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

    def _set_aside(self) -> None:
        """Moves a database that cannot be read out of the way, for a person to look at."""
        stamp = ".broken-%d" % time.time()
        for suffix in ("", "-wal", "-shm"):
            part = sibling(self.path, suffix)
            if part.exists():
                os.replace(part, sibling(part, stamp))
        log.error("%s is damaged; kept as %s%s", self.path.name, self.path.name, stamp)

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
