"""How new videos are encoded: the choices of the settings page and where they are kept."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional

from .db import Database

# What the administrator may choose on the settings page. The frame is 16:9.
HEIGHTS = {720: 1280, 1080: 1920, 1440: 2560, 2160: 3840}  # height → width
FPS_CHOICES = (30, 60)
CRF_RANGE = (16, 28)
BITRATE_RANGE_KBPS = (1000, 60000)
AUDIO_BITRATES_KBPS = (96, 128, 160, 192, 256)
X264_PRESETS = ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow")
# A sensible bitrate ceiling for each resolution: the settings page suggests it, and the
# clips of the projector check are encoded at it.
SUGGESTED_KBPS = {720: 4000, 1080: 8000, 1440: 16000, 2160: 30000}


@dataclass(frozen=True)
class Encoding:
    """How newly uploaded videos are encoded. Videos that are already processed stay as is."""

    max_height: int = 1080
    max_fps: int = 60
    max_bitrate_kbps: int = 8000
    crf: int = 21
    audio_bitrate_kbps: int = 160
    x264_preset: str = "veryfast"

    @property
    def max_width(self) -> int:
        return HEIGHTS[self.max_height]

    def problem(self) -> Optional[str]:
        """Why these values cannot be used, as text for the user; None if they can."""
        if self.max_height not in HEIGHTS:
            return "Невідома роздільність"
        if self.max_fps not in FPS_CHOICES:
            return "Невідома частота кадрів"
        if not BITRATE_RANGE_KBPS[0] <= self.max_bitrate_kbps <= BITRATE_RANGE_KBPS[1]:
            return "Бітрейт відео має бути від %d до %d кбіт/с" % BITRATE_RANGE_KBPS
        if not CRF_RANGE[0] <= self.crf <= CRF_RANGE[1]:
            return "Якість (CRF) має бути від %d до %d" % CRF_RANGE
        if self.audio_bitrate_kbps not in AUDIO_BITRATES_KBPS:
            return "Невідомий бітрейт звуку"
        if self.x264_preset not in X264_PRESETS:
            return "Невідомий пресет"
        return None


class EncodingStore:
    """Encoding chosen on the settings page; until then, the defaults from the environment."""

    KEY = "encoding"

    def __init__(self, db: Database, defaults: Encoding) -> None:
        self.db = db
        self.defaults = defaults
        self._current = defaults

    def load(self) -> None:
        # Stored values that no longer fit must not stop the service: fall back to the
        # defaults.
        self._current = self.defaults
        try:
            merged = Encoding(**{**asdict(self.defaults), **dict(self.db.get_setting(self.KEY))})
            # A value of the wrong type fails here, when it is compared with the limits.
            if not merged.problem():
                self._current = merged
        except (ValueError, TypeError):
            pass

    def get(self) -> Encoding:
        return self._current

    def set(self, encoding: Encoding) -> None:
        self.db.set_setting(self.KEY, asdict(encoding))
        self._current = encoding
