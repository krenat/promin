"""Serving the styles and scripts so that a browser never mixes old ones with new ones.

Pages name every file in static/ under a version, `/static/v-<version>/js/tv.js`. The
version changes whenever a file in static/ does, so after an update the pages ask for
addresses no browser has cached. The version is part of the path rather than a query so
that scripts importing each other by relative paths stay within the same version.
"""
from __future__ import annotations

import hashlib
import re
import time

from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from .config import STATIC_DIR

VERSION_PREFIX_RE = re.compile(r"^v-[0-9a-f]{12}[/\\]")


# How long a computed version is reused: pages are opened far more often than files change.
VERSION_TTL_SECONDS = 2.0
_cached = (float("-inf"), "")


def asset_version() -> str:
    """`v-` and twelve hex digits that change whenever a file in static/ does."""
    global _cached
    now = time.monotonic()
    if now - _cached[0] > VERSION_TTL_SECONDS:
        _cached = (now, compute_version())
    return _cached[1]


def compute_version() -> str:
    stamp = hashlib.sha256()
    for path in sorted(STATIC_DIR.rglob("*")):
        if path.is_file():
            stat = path.stat()
            name = path.relative_to(STATIC_DIR).as_posix()
            stamp.update(("%s|%d|%d;" % (name, stat.st_mtime_ns, stat.st_size)).encode("utf-8"))
    return "v-" + stamp.hexdigest()[:12]


class VersionedStatic(StaticFiles):
    """Static files that are also reachable under any version prefix."""

    def get_path(self, scope: Scope) -> str:
        return VERSION_PREFIX_RE.sub("", super().get_path(scope))
