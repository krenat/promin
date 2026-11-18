"""Where things live and what the environment asks for."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# The pages and the logo are part of the package.
PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"
MB = 1024 * 1024

MIN_TOKEN_LENGTH = 12


@dataclass
class Settings:
    data_dir: Path
    token: str = ""
    port: int = 8080
    max_upload_bytes: int = 4096 * MB
    # How much free disk space must remain after an upload.
    disk_reserve_bytes: int = 500 * MB
    max_bitrate_kbps: int = 8000
    x264_preset: str = "veryfast"

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ
        return cls(
            data_dir=Path(env.get("PROMIN_DATA_DIR", "./data")).resolve(),
            token=env.get("PROMIN_TOKEN", "").strip(),
            port=int(env.get("PROMIN_PORT", "8080")),
            max_upload_bytes=int(env.get("PROMIN_MAX_UPLOAD_MB", "4096")) * MB,
            max_bitrate_kbps=int(env.get("PROMIN_MAX_BITRATE_KBPS", "8000")),
            x264_preset=env.get("PROMIN_X264_PRESET", "veryfast"),
        )

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def videos_dir(self) -> Path:
        return self.data_dir / "videos"

    @property
    def thumbs_dir(self) -> Path:
        return self.data_dir / "thumbs"

    @property
    def tvtest_dir(self) -> Path:
        return self.data_dir / "tvtest"
