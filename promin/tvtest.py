"""The projector check: test clips, the projector's report and what it says about the settings."""
from __future__ import annotations

import asyncio
import os
import secrets
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple, TypedDict

from .config import MB
from .db import Database, sibling
from .encoding import FPS_CHOICES, HEIGHTS, SUGGESTED_KBPS
from .ffmpeg import ProcessingError, reap
from .parsing import finite

# The projector check: the projector plays a generated clip for every resolution and frame
# rate offered on the settings page and reports how it went, and measures its download speed.
TVTEST_VERSION = 1  # part of the clip file names: raise it when the clips change
TVTEST_CLIP_SECONDS = 8
TVTEST_MEASURE_SECONDS = 6  # how long playback of each clip is watched
TVTEST_SPEED_SECONDS = 6
TVTEST_SPEED_CHUNK = 4 * MB
TVTEST_MIN_ADVANCE = 0.9  # playback must keep up with the clock this closely
TVTEST_MAX_DROPPED = 0.05  # and drop at most this share of the frames
TVTEST_STALE_SECONDS = 120  # a projector silent for this long has given up
TVTEST_CLIP_TIMEOUT_SECONDS = 5 * 60
# The bitrate ceiling should leave this much of the measured speed for everything else.
NETWORK_SHARE = 0.5


class Level(TypedDict):
    """One clip of the check: a resolution and a frame rate."""

    id: str  # "1080p60"
    height: int
    fps: int
    kbps: int
    needs: Optional[str]  # the clip that has to play smoothly before this one is tried


def tvtest_levels() -> List[Level]:
    """The clips of the projector check, easiest first.

    `needs` names the clip that has to play smoothly before this one is worth trying: the
    same resolution at the lower frame rate, or the resolution below.
    """
    levels: List[Level] = []
    base_fps = min(FPS_CHOICES)
    lower = None
    for height in sorted(HEIGHTS):
        base = "%dp%d" % (height, base_fps)
        for fps in sorted(FPS_CHOICES):
            levels.append({
                "id": "%dp%d" % (height, fps), "height": height, "fps": fps,
                "kbps": SUGGESTED_KBPS[height], "needs": lower if fps == base_fps else base,
            })
        lower = base
    return levels


def tvtest_clip_name(level: Level) -> str:
    return "v%d-%s.mp4" % (TVTEST_VERSION, level["id"])


def tvtest_clip_args(ffmpeg: str, level: Level, out: Path) -> List[str]:
    """A test clip encoded the way real videos are: H.264 High at the bitrate the settings
    page suggests for this resolution, with an audio track.

    The noise keeps the encoder from spending far fewer bits than asked on a simple
    picture, so the decoder gets a realistic load. The audio is silence, but it is there:
    players drop frames to stay in sync with it.
    """
    rate = level["kbps"]
    return [
        ffmpeg, "-nostdin", "-y", "-v", "error",
        "-f", "lavfi", "-i", "testsrc2=size=%dx%d:rate=%d" % (
            HEIGHTS[level["height"]], level["height"], level["fps"]),
        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
        "-t", str(TVTEST_CLIP_SECONDS),
        "-vf", "noise=alls=12:allf=t,format=yuv420p",
        "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high",
        "-b:v", "%dk" % rate, "-maxrate", "%dk" % rate, "-bufsize", "%dk" % (rate * 2),
        "-g", str(level["fps"] * 2),
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", "-f", "mp4", str(out),
    ]


async def make_tvtest_clip(ffmpeg: str, level: Level, folder: Path) -> None:
    """Generates the clip of this level unless it is already there."""
    final = folder / tvtest_clip_name(level)
    if final.is_file():
        return
    tmp = sibling(final, ".tmp")
    proc = await asyncio.create_subprocess_exec(
        *tvtest_clip_args(ffmpeg, level, tmp),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, err = await asyncio.wait_for(proc.communicate(), TVTEST_CLIP_TIMEOUT_SECONDS)
        if proc.returncode != 0:
            raise ProcessingError(err.decode(errors="replace").strip()[-2000:])
        os.replace(tmp, final)
    finally:
        await reap(proc)
        tmp.unlink(missing_ok=True)


def clean_tvtest_report(message: Dict[str, Any], levels: List[Level]) -> Dict[str, Any]:
    """What the projector reported, as known fields of known types, with a verdict for
    every clip: ok (smooth), rough (played, but did not keep up), failed or skipped."""
    reports: Dict[str, Dict[str, Any]] = {}
    raw = message.get("clips")
    for entry in raw if isinstance(raw, list) else []:
        if isinstance(entry, dict):
            reports[str(entry.get("id"))] = entry
    clips = []
    for level in levels:
        entry = reports.get(level["id"], {})
        advance = max(0.0, min(finite(entry.get("advance")), 2.0))
        frames = max(0, int(finite(entry.get("frames"))))
        dropped = max(0, min(int(finite(entry.get("dropped"))), frames))
        clip: Dict[str, Any] = {
            "id": level["id"], "height": level["height"], "fps": level["fps"], "kbps": level["kbps"],
        }
        if entry.get("outcome") == "played":
            # Without frame counters (frames is 0) only the clock is left to judge by.
            smooth = advance >= TVTEST_MIN_ADVANCE and dropped <= frames * TVTEST_MAX_DROPPED
            clip.update(
                status="ok" if smooth else "rough",
                advance=round(advance, 3), frames=frames, dropped=dropped,
            )
        elif entry.get("outcome") == "failed":
            reason = "download" if entry.get("reason") == "download" else "play"
            clip.update(status="failed", reason=reason)
        else:
            clip.update(status="skipped")
        clips.append(clip)
    device = message.get("device")
    device = device if isinstance(device, dict) else {}
    speed = int(finite(message.get("speed_kbps")))
    return {
        "clips": clips,
        "speed_kbps": speed if speed > 0 else None,
        "device": {
            "agent": str(device.get("agent") or "")[:300],
            "screen_width": max(0, int(finite(device.get("screen_width")))),
            "screen_height": max(0, int(finite(device.get("screen_height")))),
        },
    }


def tvtest_fits(result: Dict[str, Any]) -> Dict[str, Any]:
    """What the check says about the settings: the highest resolution and frame rate that
    played smoothly (None if nothing did) and the bitrate ceiling the network allows
    (None if the speed is unknown)."""
    smooth = {clip["id"] for clip in result["clips"] if clip["status"] == "ok"}
    base_fps = min(FPS_CHOICES)
    height = None
    for candidate in sorted(HEIGHTS):
        if "%dp%d" % (candidate, base_fps) not in smooth:
            break
        height = candidate
    fits: Dict[str, Any] = {"max_height": height, "max_fps": None, "network_kbps": None}
    if height is not None:
        fits["max_fps"] = max(f for f in FPS_CHOICES if "%dp%d" % (height, f) in smooth)
    if result.get("speed_kbps"):
        fits["network_kbps"] = int(result["speed_kbps"] * NETWORK_SHARE)
    return fits


def tvtest_record(data: Any) -> Dict[str, Any]:
    """A finished projector check as TvTest.finish saved it; KeyError or TypeError if the
    stored one is not that."""
    parts: List[Tuple[Any, Sequence[str]]] = [
        (data, ("clips", "device", "fits", "finished", "speed_kbps"))
    ]
    parts += [(clip, ("id", "height", "fps", "status")) for clip in data["clips"]]
    parts += [(data["device"], ("agent",)), (data["fits"], ("max_height", "max_fps"))]
    for part, keys in parts:
        missing = [key for key in keys if key not in part]
        if missing:
            raise KeyError(", ".join(missing))
    record: Dict[str, Any] = data
    return record


class TvTest:
    """The projector check: where a running one is, and the last finished one, stored."""

    KEY = "tvtest"

    def __init__(self, db: Database) -> None:
        self.db = db
        self.levels = tvtest_levels()
        self.result: Optional[Dict[str, Any]] = None
        # idle → preparing (clips are generated) → running → idle
        self.status: Literal["idle", "preparing", "running"] = "idle"
        self.run: Optional[str] = None
        self.step = 0
        self.error: Optional[str] = None
        self.seen = 0.0  # when the projector last reported
        self.task: Optional[asyncio.Task[None]] = None

    def load(self) -> None:
        self.result = None
        with suppress(TypeError, KeyError, AttributeError):  # shown as no check yet
            self.result = tvtest_record(self.db.get_setting(self.KEY))

    def begin(self) -> str:
        self.status, self.run, self.step, self.error = "preparing", secrets.token_hex(8), 0, None
        return self.run

    def fail(self, error: str) -> None:
        self.status, self.run, self.error = "idle", None, error

    def finish(self, message: Dict[str, Any]) -> None:
        result = clean_tvtest_report(message, self.levels)
        result["fits"] = tvtest_fits(result)
        result["finished"] = time.time()
        self.db.set_setting(self.KEY, result)
        self.result = result
        self.status, self.run, self.error = "idle", None, None

    def report(self, message: Dict[str, Any]) -> None:
        """A report from the projector about the check that is running."""
        if self.status != "running" or message.get("run") != self.run:
            return
        self.seen = time.time()
        if message.get("done"):
            self.finish(message)
        elif message.get("aborted"):
            self.fail("Перевірку перервано: проектор зайнятий показом відео")
        else:
            self.step = max(0, min(int(finite(message.get("step"))), len(self.levels)))

    def view(self) -> Dict[str, Any]:
        if self.status == "running" and time.time() - self.seen > TVTEST_STALE_SECONDS:
            self.fail("Проектор перестав відповідати під час перевірки")
        return {
            "status": self.status, "step": self.step, "steps": len(self.levels),
            "error": self.error, "result": self.result,
        }
