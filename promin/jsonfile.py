"""Data files: JSON written so that a power cut or a damaged file loses as little as possible."""
from __future__ import annotations

import json
import logging
import os
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, Callable, Dict

log = logging.getLogger(__name__)


def sibling(path: Path, suffix: str) -> Path:
    return path.with_name(path.name + suffix)


def write_json(path: Path, data: Any, private: bool = False) -> None:
    """Writes a data file so that a power cut leaves a readable version behind.

    The new text reaches the disk before it replaces the file, and the previous
    version stays next to it as `.bak`.
    """
    tmp = sibling(path, ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    if private:
        os.chmod(tmp, 0o600)
    if path.exists():
        os.replace(path, sibling(path, ".bak"))
    os.replace(tmp, path)
    with suppress(OSError):  # make the renames themselves durable where the system allows
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def read_json(path: Path, check: Callable[[Any], Any]) -> Any:
    """Reads a data file written by write_json; None if there is nothing usable.

    `check` turns the parsed JSON into what the store keeps, raising ValueError,
    TypeError, KeyError or AttributeError if it does not fit. A damaged file is moved
    aside as `.broken-<time>` rather than overwritten, and the newest readable copy
    (an unfinished `.tmp`, then the `.bak`) is used instead.
    """
    if path.exists():
        try:
            return check(json.loads(path.read_text(encoding="utf-8")))
        except (ValueError, TypeError, KeyError, AttributeError):
            broken = sibling(path, ".broken-%d" % time.time())
            os.replace(path, broken)
            log.error("%s is damaged; kept as %s", path.name, broken.name)
    for copy in (sibling(path, ".tmp"), sibling(path, ".bak")):
        if not copy.exists():
            continue
        try:
            data = check(json.loads(copy.read_text(encoding="utf-8")))
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
        log.warning("%s could not be read; using %s", path.name, copy.name)
        return data
    return None


def dict_of_dicts(data: Any) -> Dict[str, Dict[str, Any]]:
    if not isinstance(data, dict) or not all(isinstance(v, dict) for v in data.values()):
        raise TypeError("expected an object of objects")
    return data


def records(*keys: str) -> Callable[[Any], Dict[str, Dict[str, Any]]]:
    """A check for read_json: an object of objects that all have these keys.

    The stores read these keys without looking, so a file that parses but lacks one
    counts as damaged instead of failing later, in the middle of a request or a start.
    """

    def check(data: Any) -> Dict[str, Dict[str, Any]]:
        for name, value in dict_of_dicts(data).items():
            missing = [key for key in keys if key not in value]
            if missing:
                raise KeyError("%s has no %s" % (name, ", ".join(missing)))
        return data

    return check
