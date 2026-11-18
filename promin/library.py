"""The video library: the list of videos, their folders and the watch queue."""
from __future__ import annotations

import json
import secrets
import sqlite3
import time
from typing import Any, List, Literal, Optional, Set, TypedDict
from urllib.parse import unquote

from .config import Settings
from .db import Database

ALLOWED_EXT = {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".3gp"}

QUEUE_MAX = 200
FOLDERS_MAX = 100
FOLDER_NAME_MAX = 60

VideoStatus = Literal["queued", "processing", "ready", "error"]


class VideoInfo(TypedDict):
    """What a finished file is, as ffprobe reads it; shown on the card."""

    width: int
    height: int
    fps: float
    bitrate_kbps: int
    video_codec: Optional[str]
    audio_codec: Optional[str]
    channels: int


class Video(TypedDict):
    id: str
    name: str
    size: Optional[int]
    status: VideoStatus
    created: float
    source: Optional[str]  # the name of the original in uploads/
    duration: Optional[float]
    thumb: bool
    thumb_v: Optional[int]
    error: Optional[str]
    folder: Optional[str]
    info: Optional[VideoInfo]


class Folder(TypedDict):
    id: str
    name: str


class QueueEntry(TypedDict):
    entry: str
    video_id: str


class QueuedVideo(TypedDict):
    """A queue entry with what a phone needs to show it."""

    entry: str
    video_id: str
    name: str
    duration: Optional[float]
    thumb: bool
    thumb_v: Optional[int]


VIDEO_COLUMNS = tuple(Video.__annotations__)
# What the pages are told about a video: everything but where its original is.
PUBLIC_KEYS = tuple(key for key in VIDEO_COLUMNS if key != "source")


def _stored(key: str, value: Any) -> Any:
    """A field of a video as its column keeps it."""
    if key == "thumb":
        return int(bool(value))
    if key == "info":
        return json.dumps(value, ensure_ascii=False) if value else None
    return value


def _video(row: sqlite3.Row) -> Video:
    return Video(
        id=row["id"], name=row["name"], size=row["size"], status=row["status"],
        created=row["created"], source=row["source"], duration=row["duration"],
        thumb=bool(row["thumb"]), thumb_v=row["thumb_v"], error=row["error"],
        folder=row["folder"], info=json.loads(row["info"]) if row["info"] else None,
    )


class VideoStore:
    """The list of videos."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def list(self) -> List[Video]:
        """Every video, newest first."""
        return [_video(row) for row in self.db.all("SELECT * FROM videos ORDER BY created DESC")]

    def get(self, video_id: str) -> Optional[Video]:
        row = self.db.one("SELECT * FROM videos WHERE id = ?", (video_id,))
        return _video(row) if row else None

    def add(self, item: Video) -> None:
        self.db.run(
            "INSERT OR REPLACE INTO videos (%s) VALUES (%s)"
            % (", ".join(VIDEO_COLUMNS), ", ".join("?" * len(VIDEO_COLUMNS))),
            [_stored(key, item.get(key)) for key in VIDEO_COLUMNS],
        )

    def update(self, video_id: str, **fields: Any) -> bool:
        """Changes these fields of a video; False if there is no such video."""
        unknown = set(fields) - set(VIDEO_COLUMNS) | set(fields) & {"id"}
        if unknown or not fields:
            raise KeyError("A video has no %s to update" % (", ".join(sorted(unknown)) or "fields"))
        changed = self.db.run(
            "UPDATE videos SET %s WHERE id = ?" % ", ".join(key + " = ?" for key in fields),
            [_stored(key, value) for key, value in fields.items()] + [video_id],
        )
        return changed > 0

    def remove(self, video_id: str) -> bool:
        return bool(self.remove_many([video_id]))

    def remove_many(self, video_ids: List[str]) -> List[str]:
        """Removes these videos together; returns the ids that were there."""
        with self.db.transaction() as conn:
            removed = [
                video_id for video_id in video_ids
                if conn.execute("DELETE FROM videos WHERE id = ?", (video_id,)).rowcount
            ]
        return removed

    def clear_folder(self, folder_id: str) -> None:
        """Leaves the videos of a folder without one."""
        self.db.run("UPDATE videos SET folder = NULL WHERE folder = ?", (folder_id,))


def clean_name(raw: str) -> str:
    """Display name: no path, limited length."""
    name = unquote(raw).replace("\\", "/").split("/")[-1].strip()
    return name[:200] or "video"


def remove_files(settings: Settings, video_id: str) -> None:
    for folder in (settings.uploads_dir, settings.videos_dir, settings.thumbs_dir):
        for path in folder.glob(video_id + ".*"):
            path.unlink(missing_ok=True)


class FolderStore:
    """Folders of the library, one level deep. A video names its folder; a folder lists nothing."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def list(self) -> List[Folder]:
        items = [Folder(id=row["id"], name=row["name"]) for row in self.db.all("SELECT * FROM folders")]
        return sorted(items, key=lambda f: f["name"].casefold())

    def exists(self, folder_id: Optional[str]) -> bool:
        if folder_id is None:
            return False
        return self.db.one("SELECT 1 FROM folders WHERE id = ?", (folder_id,)) is not None

    @staticmethod
    def _taken(conn: sqlite3.Connection, name: str, other_than: Optional[str] = None) -> bool:
        # Compared here, not in SQL: SQLite folds the case of Latin letters only.
        return any(
            row["name"].casefold() == name.casefold()
            for row in conn.execute("SELECT id, name FROM folders") if row["id"] != other_than
        )

    def add(self, name: str) -> str:
        """Creates a folder and returns its id; raises ValueError with text for the user."""
        with self.db.transaction() as conn:
            if conn.execute("SELECT COUNT(*) FROM folders").fetchone()[0] >= FOLDERS_MAX:
                raise ValueError("Тек може бути щонайбільше %d" % FOLDERS_MAX)
            if self._taken(conn, name):
                raise ValueError("Така тека вже є")
            folder_id = secrets.token_hex(6)
            conn.execute(
                "INSERT INTO folders (id, name, created) VALUES (?, ?, ?)",
                (folder_id, name, time.time()),
            )
            return folder_id

    def rename(self, folder_id: str, name: str) -> bool:
        """False if there is no such folder; ValueError if the name is taken."""
        with self.db.transaction() as conn:
            if not self.exists(folder_id):
                return False
            if self._taken(conn, name, other_than=folder_id):
                raise ValueError("Така тека вже є")
            conn.execute("UPDATE folders SET name = ? WHERE id = ?", (name, folder_id))
            return True

    def remove(self, folder_id: str) -> bool:
        return self.db.run("DELETE FROM folders WHERE id = ?", (folder_id,)) > 0


def clean_folder_name(raw: str) -> str:
    """A folder name as typed, tidied up; ValueError with text for the user if it cannot be one."""
    name = " ".join("".join(ch for ch in raw if ch.isprintable()).split())
    if not name:
        raise ValueError("Назва теки не може бути порожньою")
    if len(name) > FOLDER_NAME_MAX:
        raise ValueError("Назва теки задовга: щонайбільше %d символів" % FOLDER_NAME_MAX)
    return name


class QueueStore:
    """The shared watch queue of the projector: what plays next, in order.

    Each entry has its own id, so one video can be queued twice and an entry can be
    moved or removed without touching its twin.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    @staticmethod
    def _entries(conn: sqlite3.Connection) -> List[QueueEntry]:
        return [
            QueueEntry(entry=row["entry"], video_id=row["video_id"])
            for row in conn.execute("SELECT entry, video_id FROM queue ORDER BY position")
        ]

    def list(self) -> List[QueueEntry]:
        with self.db.reading() as conn:
            return self._entries(conn)

    def playable(self) -> List[QueuedVideo]:
        """The queue as phones show it: its entries whose videos can be played, in order."""
        return [
            QueuedVideo(
                entry=row["entry"], video_id=row["id"], name=row["name"],
                duration=row["duration"], thumb=bool(row["thumb"]), thumb_v=row["thumb_v"],
            )
            for row in self.db.all(
                "SELECT queue.entry, videos.id, videos.name, videos.duration, videos.thumb,"
                " videos.thumb_v FROM queue JOIN videos ON videos.id = queue.video_id"
                " WHERE videos.status = 'ready' ORDER BY queue.position"
            )
        ]

    def add(self, video_id: str) -> str:
        """Appends a video and returns the new entry id; raises ValueError with text for
        the user if the queue is full."""
        with self.db.transaction() as conn:
            if conn.execute("SELECT COUNT(*) FROM queue").fetchone()[0] >= QUEUE_MAX:
                raise ValueError("У черзі вже %d відео" % QUEUE_MAX)
            entry = secrets.token_hex(6)
            # Without a position the row gets the one after the last.
            conn.execute("INSERT INTO queue (entry, video_id) VALUES (?, ?)", (entry, video_id))
            return entry

    def remove(self, entry: str) -> bool:
        return self.db.run("DELETE FROM queue WHERE entry = ?", (entry,)) > 0

    def remove_videos(self, video_ids: Set[str]) -> bool:
        """Drops every entry of these videos. True if the queue changed."""
        with self.db.transaction() as conn:
            changed = [
                conn.execute("DELETE FROM queue WHERE video_id = ?", (video_id,)).rowcount
                for video_id in video_ids
            ]
        return any(changed)

    def move(self, entry: str, position: int) -> bool:
        with self.db.transaction() as conn:
            entries = self._entries(conn)
            index = next((i for i, e in enumerate(entries) if e["entry"] == entry), None)
            if index is None:
                return False
            item = entries.pop(index)
            entries.insert(max(0, min(position, len(entries))), item)
            conn.execute("DELETE FROM queue")
            conn.executemany(
                "INSERT INTO queue (position, entry, video_id) VALUES (?, ?, ?)",
                [(i, e["entry"], e["video_id"]) for i, e in enumerate(entries, 1)],
            )
            return True

    def clear(self) -> None:
        self.db.run("DELETE FROM queue")

    def pop(self) -> Optional[QueueEntry]:
        with self.db.transaction() as conn:
            entries = self._entries(conn)
            if not entries:
                return None
            conn.execute("DELETE FROM queue WHERE entry = ?", (entries[0]["entry"],))
            return entries[0]
