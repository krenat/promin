"""The video library on disk: the list of videos, their folders and the watch queue."""
from __future__ import annotations

import secrets
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import unquote

from .config import Settings
from .jsonfile import read_json, records, write_json

ALLOWED_EXT = {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".3gp"}
PUBLIC_KEYS = (
    "id", "name", "size", "status", "created", "duration", "thumb", "thumb_v", "error", "folder",
    "info",
)

QUEUE_MAX = 200
FOLDERS_MAX = 100
FOLDER_NAME_MAX = 60


def video_records(data: Any) -> Dict[str, Dict[str, Any]]:
    for item in records("id", "name", "status", "created")(data).values():
        # The list is sorted by this, and bool is a number to Python.
        if isinstance(item["created"], bool) or not isinstance(item["created"], (int, float)):
            raise TypeError("created is not a number")
    return data


class VideoStore:
    """Video list in a JSON file. Writes are locked and atomic."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._items: Dict[str, Dict[str, Any]] = {}

    def load(self) -> None:
        with self._lock:
            self._items = read_json(self.path, video_records) or {}

    def _save(self) -> None:
        write_json(self.path, self._items)

    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            items = [dict(item) for item in self._items.values()]
        return sorted(items, key=lambda item: item["created"], reverse=True)

    def get(self, video_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            item = self._items.get(video_id)
            return dict(item) if item else None

    def add(self, item: Dict[str, Any]) -> None:
        with self._lock:
            self._items[item["id"]] = item
            self._save()

    def update(self, video_id: str, **fields: Any) -> bool:
        with self._lock:
            if video_id not in self._items:
                return False
            self._items[video_id].update(fields)
            self._save()
            return True

    def remove(self, video_id: str) -> bool:
        return bool(self.remove_many([video_id]))

    def remove_many(self, video_ids: List[str]) -> List[str]:
        """Removes these videos with a single write; returns the ids that were there."""
        with self._lock:
            removed = [video_id for video_id in video_ids if video_id in self._items]
            for video_id in removed:
                del self._items[video_id]
            if removed:
                self._save()
            return removed

    def clear_folder(self, folder_id: str) -> None:
        """Leaves the videos of a folder without one, with a single write."""
        with self._lock:
            inside = [item for item in self._items.values() if item.get("folder") == folder_id]
            for item in inside:
                item["folder"] = None
            if inside:
                self._save()


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

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._folders: Dict[str, Dict[str, Any]] = {}

    def load(self) -> None:
        def check(data: Any) -> Dict[str, Dict[str, Any]]:
            return {
                str(key): {"name": str(value["name"]), "created": float(value["created"])}
                for key, value in data.items()
            }

        with self._lock:
            # Videos keep their folder ids, so the folders come back once the file is fixed.
            self._folders = read_json(self.path, check) or {}

    def _save(self) -> None:
        write_json(self.path, self._folders)

    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            items = [{"id": key, "name": f["name"]} for key, f in self._folders.items()]
        return sorted(items, key=lambda f: f["name"].casefold())

    def exists(self, folder_id: Optional[str]) -> bool:
        with self._lock:
            return folder_id in self._folders

    def _taken(self, name: str, other_than: Optional[str] = None) -> bool:
        return any(
            f["name"].casefold() == name.casefold()
            for key, f in self._folders.items() if key != other_than
        )

    def add(self, name: str) -> str:
        """Creates a folder and returns its id; raises ValueError with text for the user."""
        with self._lock:
            if len(self._folders) >= FOLDERS_MAX:
                raise ValueError("Тек може бути щонайбільше %d" % FOLDERS_MAX)
            if self._taken(name):
                raise ValueError("Така тека вже є")
            folder_id = secrets.token_hex(6)
            self._folders[folder_id] = {"name": name, "created": time.time()}
            self._save()
            return folder_id

    def rename(self, folder_id: str, name: str) -> bool:
        """False if there is no such folder; ValueError if the name is taken."""
        with self._lock:
            if folder_id not in self._folders:
                return False
            if self._taken(name, other_than=folder_id):
                raise ValueError("Така тека вже є")
            self._folders[folder_id]["name"] = name
            self._save()
            return True

    def remove(self, folder_id: str) -> bool:
        with self._lock:
            if folder_id not in self._folders:
                return False
            del self._folders[folder_id]
            self._save()
            return True


def clean_folder_name(raw: str) -> str:
    """A folder name as typed, tidied up; ValueError with text for the user if it cannot be one."""
    name = " ".join("".join(ch for ch in raw if ch.isprintable()).split())
    if not name:
        raise ValueError("Назва теки не може бути порожньою")
    if len(name) > FOLDER_NAME_MAX:
        raise ValueError("Назва теки задовга: щонайбільше %d символів" % FOLDER_NAME_MAX)
    return name


class QueueStore:
    """The shared watch queue of the projector: what plays next, in order, in a JSON file.

    Each entry has its own id, so one video can be queued twice and an entry can be
    moved or removed without touching its twin.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._entries: List[Dict[str, str]] = []

    def load(self) -> None:
        def check(data: Any) -> List[Dict[str, str]]:
            if not isinstance(data, list):
                raise TypeError("expected a list")
            return [{"entry": str(e["entry"]), "video_id": str(e["video_id"])} for e in data]

        with self._lock:
            # A lost queue is a small loss; a service that will not start is a big one.
            self._entries = read_json(self.path, check) or []

    def _save(self) -> None:
        write_json(self.path, self._entries)

    def list(self) -> List[Dict[str, str]]:
        with self._lock:
            return [dict(entry) for entry in self._entries]

    def add(self, video_id: str) -> str:
        """Appends a video and returns the new entry id; raises ValueError with text for
        the user if the queue is full."""
        with self._lock:
            if len(self._entries) >= QUEUE_MAX:
                raise ValueError("У черзі вже %d відео" % QUEUE_MAX)
            entry = secrets.token_hex(6)
            self._entries.append({"entry": entry, "video_id": video_id})
            self._save()
            return entry

    def remove(self, entry: str) -> bool:
        with self._lock:
            kept = [e for e in self._entries if e["entry"] != entry]
            if len(kept) == len(self._entries):
                return False
            self._entries = kept
            self._save()
            return True

    def remove_videos(self, video_ids: set) -> bool:
        """Drops every entry of these videos. True if the queue changed."""
        with self._lock:
            kept = [e for e in self._entries if e["video_id"] not in video_ids]
            if len(kept) == len(self._entries):
                return False
            self._entries = kept
            self._save()
            return True

    def move(self, entry: str, position: int) -> bool:
        with self._lock:
            index = next((i for i, e in enumerate(self._entries) if e["entry"] == entry), None)
            if index is None:
                return False
            item = self._entries.pop(index)
            position = max(0, min(position, len(self._entries)))
            self._entries.insert(position, item)
            self._save()
            return True

    def clear(self) -> None:
        with self._lock:
            self._entries = []
            self._save()

    def pop(self) -> Optional[Dict[str, str]]:
        with self._lock:
            if not self._entries:
                return None
            entry = self._entries.pop(0)
            self._save()
            return entry
