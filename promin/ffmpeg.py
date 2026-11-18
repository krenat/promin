"""What to ask of ffmpeg: the plan read from ffprobe, the commands, the HDR filters."""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, TypedDict

from .encoding import Encoding
from .library import VideoInfo

# Codecs that need fewer bits than H.264 for the same picture: when transcoding from them,
# the bitrate ceiling is the source bitrate with this headroom.
EFFICIENT_CODECS = {"hevc", "av1", "vp9", "vvc"}
EFFICIENT_CODEC_HEADROOM = 1.8
MIN_RATE_KBPS = 300  # a ceiling below this only makes the picture fall apart
HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}  # PQ (HDR10) and HLG

# HDR → SDR chains. The first needs ffmpeg with libzimg, the second a recent swscale.
HDR_FILTER_ZSCALE = (
    "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
    "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv"
)
HDR_FILTER_SWSCALE = (
    "scale=out_transfer=linear:out_primaries=bt709,format=gbrpf32le,"
    "tonemap=tonemap=hable:desat=0,"
    "scale=out_transfer=bt709:out_color_matrix=bt709:out_range=tv"
)


class Plan(TypedDict):
    """How much work a video needs, as plan_encode decided."""

    # remux: only repackage; audio: re-encode only the audio; transcode: everything.
    mode: Literal["remux", "audio", "transcode"]
    duration: float
    fps: float
    hdr: bool
    max_rate_kbps: int


class ProcessingError(Exception):
    """A processing error whose text is shown to the user."""


def parse_fps(value: Optional[str]) -> float:
    try:
        num, _, den = (value or "").partition("/")
        return float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        return 0.0


def stream_rotation(stream: Dict[str, Any]) -> int:
    for side in stream.get("side_data_list") or []:
        if "rotation" in side:
            return int(round(float(side["rotation"]))) % 360
    try:
        return int(float((stream.get("tags") or {}).get("rotate", 0))) % 360
    except ValueError:
        return 0


def plan_encode(probe: Dict[str, Any], encoding: Encoding) -> Plan:
    """Decides from the ffprobe output how much work is needed.

    remux: the file already fits, only repackage it;
    audio: the video fits, re-encode only the audio;
    transcode: re-encode everything.
    """
    streams = probe.get("streams") or []
    video = next(
        (
            s
            for s in streams
            if s.get("codec_type") == "video"
            and not (s.get("disposition") or {}).get("attached_pic")
        ),
        None,
    )
    if video is None:
        raise ProcessingError("У файлі немає відеодоріжки")
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = probe.get("format") or {}

    def number(*values: Any) -> float:
        for value in values:
            try:
                if float(value) > 0:
                    return float(value)
            except (TypeError, ValueError):
                continue
        return 0.0

    duration = number(fmt.get("duration"), video.get("duration"))
    fps = parse_fps(video.get("avg_frame_rate")) or parse_fps(video.get("r_frame_rate"))
    bitrate = number(video.get("bit_rate"), fmt.get("bit_rate"))
    hdr = video.get("color_transfer") in HDR_TRANSFERS

    # Leave the video alone only when the projector is sure to play it as is.
    video_ok = (
        video.get("codec_name") == "h264"
        and video.get("pix_fmt") == "yuv420p"
        and not hdr
        and stream_rotation(video) == 0
        and 0 < int(video.get("width") or 0) <= encoding.max_width
        and 0 < int(video.get("height") or 0) <= encoding.max_height
        and 0 < fps <= encoding.max_fps + 0.5
        and 0 < bitrate <= encoding.max_bitrate_kbps * 1000 * 1.25
    )
    # Keep the audio as is if it is mono or stereo AAC.
    audio_ok = audio is None or (
        audio.get("codec_name") == "aac" and int(audio.get("channels") or 2) <= 2
    )
    mode: Literal["remux", "audio", "transcode"]
    if video_ok and audio_ok:
        mode = "remux"
    elif video_ok:
        mode = "audio"
    else:
        mode = "transcode"
    # A transcoded file gains nothing from more bits than the source had, so the ceiling
    # follows the source bitrate; more efficient codecs get headroom to keep their quality.
    max_rate = encoding.max_bitrate_kbps
    if bitrate > 0:
        headroom = EFFICIENT_CODEC_HEADROOM if video.get("codec_name") in EFFICIENT_CODECS else 1.0
        max_rate = min(max_rate, max(MIN_RATE_KBPS, int(bitrate / 1000 * headroom)))
    return {
        "mode": mode,
        "duration": duration,
        "fps": fps,
        "hdr": hdr,
        "max_rate_kbps": max_rate,
    }


def video_info(probe: Dict[str, Any]) -> Optional[VideoInfo]:
    """What the finished file is, for the card: frame size, frame rate, bitrate, codecs."""
    streams = probe.get("streams") or []
    video = next(
        (
            s for s in streams
            if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")
        ),
        None,
    )
    if video is None:
        return None
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fps = parse_fps(video.get("avg_frame_rate")) or parse_fps(video.get("r_frame_rate"))
    bitrate = 0.0
    for value in (video.get("bit_rate"), (probe.get("format") or {}).get("bit_rate")):
        try:
            bitrate = float(value or "")
            break
        except (TypeError, ValueError):
            continue
    try:
        width, height = int(video.get("width") or 0), int(video.get("height") or 0)
        channels = int(audio.get("channels") or 0) if audio else 0
    except (TypeError, ValueError):
        return None
    return {
        "width": width,
        "height": height,
        "fps": round(fps, 2),
        "bitrate_kbps": int(bitrate / 1000),
        "video_codec": video.get("codec_name"),
        "audio_codec": audio.get("codec_name") if audio else None,
        "channels": channels,
    }


def build_ffmpeg_args(
    ffmpeg: str,
    src: Path,
    dst: Path,
    plan: Plan,
    encoding: Encoding,
    hdr_filter: Optional[str],
) -> List[str]:
    args = [ffmpeg, "-nostdin", "-y", "-v", "error", "-i", str(src)]
    args += ["-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn"]
    audio_rate = "%dk" % encoding.audio_bitrate_kbps
    if plan["mode"] == "remux":
        args += ["-c", "copy"]
    elif plan["mode"] == "audio":
        # The picture is copied losslessly; only the audio is re-encoded.
        args += ["-c:v", "copy", "-c:a", "aac", "-b:a", audio_rate, "-ac", "2"]
    else:
        tone_map = hdr_filter if plan["hdr"] else None
        # Fit into the frame without upscaling; ffmpeg applies rotation by itself.
        filters = [
            "scale=w='min(%d,iw)':h='min(%d,ih)'"
            ":force_original_aspect_ratio=decrease:force_divisible_by=2"
            % (encoding.max_width, encoding.max_height)
        ]
        # Tone mapping goes after downscaling: on 4K it is three times slower.
        if tone_map:
            filters.append(tone_map)
        if plan["fps"] > encoding.max_fps + 0.5:
            filters.append("fps=%d" % encoding.max_fps)
        filters.append("format=yuv420p")
        rate = plan["max_rate_kbps"]
        args += [
            "-vf", ",".join(filters),
            "-c:v", "libx264",
            "-preset", encoding.x264_preset,
            "-profile:v", "high",
            "-crf", str(encoding.crf),
            "-maxrate", "%dk" % rate,
            "-bufsize", "%dk" % (rate * 2),
            # A keyframe every 2 seconds, for fast seeking.
            "-force_key_frames", "expr:gte(t,n_forced*2)",
            "-c:a", "aac", "-b:a", audio_rate, "-ac", "2",
        ]
        if tone_map:
            args += ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"]
    # Strip metadata: phone videos often carry a location.
    args += ["-map_metadata", "-1", "-movflags", "+faststart"]
    args += ["-progress", "pipe:1", "-nostats", str(dst)]
    return args


def detect_hdr_filter(ffmpeg: str) -> Optional[str]:
    """Which HDR → SDR chain the installed ffmpeg supports."""

    def output(*args: str) -> str:
        try:
            result = subprocess.run(
                [ffmpeg, "-hide_banner", *args], capture_output=True, text=True, timeout=20
            )
            return result.stdout
        except (OSError, subprocess.SubprocessError):
            return ""

    filters = output("-filters")
    if " tonemap " not in filters:
        return None
    if " zscale " in filters:
        return HDR_FILTER_ZSCALE
    if "out_transfer" in output("-h", "filter=scale"):
        return HDR_FILTER_SWSCALE
    return None


def thumbnail_args(ffmpeg: str, video: Path, out: Path, duration: float) -> List[str]:
    """Thumbnail frame: not from the very start, which is often black or a title card."""
    if duration > 20:
        # Take the most representative frame of two seconds, a tenth of the way in.
        seek, filters = duration * 0.1, "thumbnail=50,scale=640:-2"
    else:
        seek, filters = min(1.0, duration / 2), "scale=640:-2"
    return [
        ffmpeg, "-nostdin", "-y", "-v", "error",
        "-ss", "%.2f" % seek, "-i", str(video),
        "-frames:v", "1", "-vf", filters, "-q:v", "5", "-f", "image2", str(out),
    ]


async def reap(proc: asyncio.subprocess.Process) -> None:
    """Does not leave the process running if processing was interrupted."""
    if proc.returncode is None:
        proc.kill()
        await proc.wait()
