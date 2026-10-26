"""ffprobe based media analysis (resolution, frame rate, colour, audio)."""

from __future__ import annotations

import json
import shutil
import subprocess
from fractions import Fraction
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict


class ProbeError(RuntimeError):
    """Raised when a file cannot be analysed or lacks a usable video stream."""


class ToolMissingError(RuntimeError):
    """Raised when ffmpeg/ffprobe (or a required encoder) is not available."""


# Standard rates that slightly-off container rates (29.97002997, 23.9760...) snap to.
STANDARD_RATES: tuple[Fraction, ...] = (
    Fraction(24000, 1001),
    Fraction(24),
    Fraction(25),
    Fraction(30000, 1001),
    Fraction(30),
    Fraction(48000, 1001),
    Fraction(48),
    Fraction(50),
    Fraction(60000, 1001),
    Fraction(60),
    Fraction(100),
    Fraction(120000, 1001),
    Fraction(120),
)


class VideoStreamInfo(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    index: int
    codec_name: str
    width: int
    height: int
    """Display dimensions (after rotation and SAR are taken into account)."""
    frame_width: int
    frame_height: int
    """Pixel dimensions of decoded frames after FFmpeg's autorotate (crop space)."""
    coded_width: int
    coded_height: int
    fps: Fraction
    pix_fmt: str
    rotation: int
    sar: Fraction
    color_space: str | None
    color_primaries: str | None
    color_transfer: str | None
    color_range: str | None
    nb_frames: int | None

    @property
    def is_rgb(self) -> bool:
        return self.pix_fmt.startswith(("rgb", "bgr", "gbr", "argb", "abgr", "0rgb", "0bgr", "rgba", "bgra"))

    @property
    def is_hdr(self) -> bool:
        return self.color_transfer in {"smpte2084", "arib-std-b67"}


class AudioStreamInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    index: int
    codec_name: str
    sample_rate: int
    channels: int
    channel_layout: str | None


class MediaInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: Path
    format_name: str
    duration: float
    video: VideoStreamInfo
    audio: AudioStreamInfo | None

    @property
    def has_audio(self) -> bool:
        return self.audio is not None


@lru_cache(maxsize=None)
def find_tool(name: str) -> str:
    """Return the absolute path of ``ffmpeg``/``ffprobe`` or raise ToolMissingError."""
    path = shutil.which(name)
    if path is None:
        raise ToolMissingError(f"'{name}' was not found in PATH. Install FFmpeg (>= 5.1) and retry.")
    return path


@lru_cache(maxsize=None)
def ffmpeg_buildconf() -> str:
    result = subprocess.run(
        [find_tool("ffmpeg"), "-hide_banner", "-buildconf"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout + result.stderr


@lru_cache(maxsize=None)
def ffmpeg_encoders() -> frozenset[str]:
    result = subprocess.run(
        [find_tool("ffmpeg"), "-hide_banner", "-encoders"],
        capture_output=True,
        text=True,
        check=False,
    )
    names: set[str] = set()
    for line in result.stdout.splitlines():
        parts = line.split()
        # Encoder rows look like: " V....D libx264   libx264 H.264 / AVC ..."
        if len(parts) >= 2 and len(parts[0]) == 6 and parts[0][0] in "VAS":
            names.add(parts[1])
    return frozenset(names)


def has_soxr() -> bool:
    return "--enable-libsoxr" in ffmpeg_buildconf()


def require_encoder(name: str) -> None:
    if name not in ffmpeg_encoders():
        raise ToolMissingError(f"This FFmpeg build has no '{name}' encoder.")


def _parse_rate(value: str | None) -> Fraction | None:
    if not value or value in {"0/0", "N/A"}:
        return None
    try:
        rate = Fraction(value)
    except (ValueError, ZeroDivisionError):
        return None
    return rate if rate > 0 else None


def normalize_fps(rate: Fraction) -> Fraction:
    """Snap to the nearest broadcast standard within 0.05%; otherwise keep a sane rational.

    30/1 and 30000/1001 are only 0.1% apart, so the tolerance must stay well
    below that and the *nearest* candidate has to win.
    """
    nearest = min(STANDARD_RATES, key=lambda standard: abs(rate - standard))
    if abs(rate - nearest) / nearest <= Fraction(5, 10_000):
        return nearest
    return rate.limit_denominator(1001)


def _pick_fps(stream: dict[str, Any]) -> Fraction:
    avg = _parse_rate(stream.get("avg_frame_rate"))
    real = _parse_rate(stream.get("r_frame_rate"))
    # For constant-rate renders r_frame_rate is the exact nominal rate. For VFR
    # captures it can be a timebase-derived ceiling (e.g. 90000/1), so fall
    # back to avg_frame_rate whenever the two disagree by more than 1%.
    if real is not None and avg is not None and abs(real - avg) / avg <= Fraction(1, 100):
        rate: Fraction | None = real
    elif avg is not None and avg <= 240:
        rate = avg
    else:
        rate = real
    if rate is None or rate > 240:
        raise ProbeError("could not determine the video frame rate")
    return normalize_fps(rate)


def _rotation(stream: dict[str, Any]) -> int:
    for side_data in stream.get("side_data_list", []) or []:
        if "rotation" in side_data:
            try:
                return int(round(float(side_data["rotation"]))) % 360
            except (TypeError, ValueError):
                continue
    rotate_tag = (stream.get("tags") or {}).get("rotate")
    if rotate_tag is not None:
        try:
            return int(rotate_tag) % 360
        except ValueError:
            return 0
    return 0


def _clean_tag(value: Any) -> str | None:
    if not isinstance(value, str) or value in {"", "unknown", "unspecified", "reserved"}:
        return None
    return value


def _parse_sar(value: str | None) -> Fraction:
    if not value or value in {"0:1", "N/A"}:
        return Fraction(1)
    try:
        num, den = (int(part) for part in value.split(":"))
        return Fraction(num, den) if num > 0 and den > 0 else Fraction(1)
    except ValueError:
        return Fraction(1)


def _float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def run_ffprobe(path: Path) -> dict[str, Any]:
    cmd = [
        find_tool("ffprobe"),
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        message = result.stderr.strip() or f"ffprobe exited with code {result.returncode}"
        raise ProbeError(f"{path.name}: {message}")
    try:
        data: dict[str, Any] = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"{path.name}: unreadable ffprobe output") from exc
    return data


def probe(path: Path) -> MediaInfo:
    """Analyse ``path`` and return the first real video stream and first audio stream."""
    if not path.is_file():
        raise ProbeError(f"input file does not exist: {path}")
    data = run_ffprobe(path)
    streams: list[dict[str, Any]] = data.get("streams", [])
    fmt: dict[str, Any] = data.get("format", {})

    video_streams = [
        s
        for s in streams
        if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")
    ]
    if not video_streams:
        raise ProbeError(f"{path.name}: no video stream found")
    vs = video_streams[0]

    coded_w = int(vs.get("width") or 0)
    coded_h = int(vs.get("height") or 0)
    if coded_w <= 0 or coded_h <= 0:
        raise ProbeError(f"{path.name}: invalid video dimensions")

    rotation = _rotation(vs)
    sar = _parse_sar(vs.get("sample_aspect_ratio"))
    display_w = int(round(coded_w * sar)) if sar != 1 else coded_w
    display_h = coded_h
    frame_w, frame_h = coded_w, coded_h
    if rotation in (90, 270):
        display_w, display_h = display_h, display_w
        frame_w, frame_h = frame_h, frame_w

    nb_frames_raw = vs.get("nb_frames")
    nb_frames = int(nb_frames_raw) if isinstance(nb_frames_raw, str) and nb_frames_raw.isdigit() else None

    video = VideoStreamInfo(
        index=int(vs["index"]),
        codec_name=str(vs.get("codec_name", "unknown")),
        width=display_w,
        height=display_h,
        frame_width=frame_w,
        frame_height=frame_h,
        coded_width=coded_w,
        coded_height=coded_h,
        fps=_pick_fps(vs),
        pix_fmt=str(vs.get("pix_fmt", "yuv420p")),
        rotation=rotation,
        sar=sar,
        color_space=_clean_tag(vs.get("color_space")),
        color_primaries=_clean_tag(vs.get("color_primaries")),
        color_transfer=_clean_tag(vs.get("color_transfer")),
        color_range=_clean_tag(vs.get("color_range")),
        nb_frames=nb_frames,
    )

    audio: AudioStreamInfo | None = None
    for s in streams:
        if s.get("codec_type") == "audio":
            audio = AudioStreamInfo(
                index=int(s["index"]),
                codec_name=str(s.get("codec_name", "unknown")),
                sample_rate=int(s.get("sample_rate") or 48000),
                channels=int(s.get("channels") or 2),
                channel_layout=_clean_tag(s.get("channel_layout")),
            )
            break

    duration = _float(fmt.get("duration")) or _float(vs.get("duration"))
    if duration is None and nb_frames:
        duration = float(nb_frames / video.fps)

    return MediaInfo(
        path=path,
        format_name=str(fmt.get("format_name", "unknown")),
        duration=duration or 0.0,
        video=video,
        audio=audio,
    )
