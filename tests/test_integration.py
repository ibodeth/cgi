"""End-to-end encode of a synthetic clip. Skipped when FFmpeg is not installed."""

from __future__ import annotations

import json
import shutil
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from cinemaster.engine import master_file
from cinemaster.presets import resolve_config

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="ffmpeg/ffprobe not installed"
)


def _make_source(path: Path, audio: bool) -> None:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "gradients=s=320x240:r=30:d=1"]
    if audio:
        cmd += ["-f", "lavfi", "-i", "sine=f=440:d=1:sample_rate=44100"]
    cmd += ["-c:v", "libx264", "-metadata", "title=private-scene-name"]
    if audio:
        cmd += ["-c:a", "aac"]
    subprocess.run([*cmd, str(path)], check=True)


def _streams(path: Path) -> list[dict[str, Any]]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(out.stdout)
    assert "title" not in data["format"].get("tags", {})
    streams: list[dict[str, Any]] = data["streams"]
    return streams


def _container_headers(path: Path) -> bytes:
    """All top-level boxes except the mdat payload (compressed data can match any 4 bytes)."""
    data = path.read_bytes()
    headers, offset = bytearray(), 0
    while offset + 8 <= len(data):
        size, box_type = struct.unpack(">I4s", data[offset : offset + 8])
        if size == 1:
            size = struct.unpack(">Q", data[offset + 8 : offset + 16])[0]
        size = size or len(data) - offset
        headers += data[offset : offset + 16] if box_type == b"mdat" else data[offset : offset + size]
        offset += size
    return bytes(headers)


def _sei_count(path: Path) -> int:
    trace = subprocess.run(
        ["ffmpeg", "-v", "trace", "-i", str(path), "-c", "copy", "-bsf:v", "trace_headers", "-f", "null", "-"],
        capture_output=True,
        text=True,
        check=True,
    )
    return trace.stderr.count("Supplemental Enhancement Information")


@pytest.mark.parametrize("container", ["mp4", "mov"])
def test_master_file_end_to_end(tmp_path: Path, container: str) -> None:
    src = tmp_path / "render.mp4"
    dst = tmp_path / f"master.{container}"
    _make_source(src, audio=True)
    cfg = resolve_config("cinema", {"encoder_preset": "ultrafast", "seed": 5})

    result = master_file(src, dst, cfg)
    assert result.ok, result.message

    video, audio = _streams(dst)
    assert (video["codec_name"], video["width"], video["height"]) == ("h264", 320, 240)
    assert video["pix_fmt"] == "yuv420p" and video["color_space"] == "bt709"
    assert (audio["codec_name"], audio["sample_rate"], audio["channels"]) == ("aac", "48000", 2)
    headers = _container_headers(dst)
    for signature in (b"Lavf", b"Lavc", b"libx264", b"FFMP", b"private-scene-name"):
        assert signature not in headers
    # The x264 option dump lives in an SEI inside mdat; check the whole file for it.
    assert b"x264 - core" not in dst.read_bytes()
    assert _sei_count(dst) == 0
    if container == "mov":
        assert video["tags"]["vendor_id"] == "appl"
    assert not list(tmp_path.glob(".*.part.*"))


def test_video_only_and_keep_sei(tmp_path: Path) -> None:
    src = tmp_path / "silent.mp4"
    dst = tmp_path / "silent_master.mp4"
    _make_source(src, audio=False)
    cfg = resolve_config("subtle", {"encoder_preset": "ultrafast", "strip_sei": False, "fps": "24"})

    result = master_file(src, dst, cfg)
    assert result.ok, result.message
    streams = _streams(dst)
    assert [s["codec_type"] for s in streams] == ["video"]
    assert streams[0]["r_frame_rate"] == "24/1"
    assert _sei_count(dst) > 0


def test_existing_output_is_skipped(tmp_path: Path) -> None:
    src = tmp_path / "a.mp4"
    dst = tmp_path / "b.mp4"
    _make_source(src, audio=False)
    dst.write_bytes(b"keep me")
    result = master_file(src, dst, resolve_config("subtle"))
    assert result.skipped and dst.read_bytes() == b"keep me"


def test_cli_prores_pcm_surround_source(tmp_path: Path) -> None:
    """Interleaved ProRes + 5.1 PCM MOV: used to stall when both branches shared one graph."""
    src = tmp_path / "render [v2].mov"
    dst = tmp_path / "out" / "render.mp4"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "gradients=s=321x241:r=25:d=1",
            "-f", "lavfi", "-i", "anoisesrc=d=1:c=pink:r=44100",
            "-filter_complex", "[1]pan=5.1|c0=c0|c1=c0|c2=c0|c3=c0|c4=c0|c5=c0[a]",
            "-map", "0", "-map", "[a]", "-t", "1",
            "-c:v", "prores_ks", "-profile:v", "4", "-pix_fmt", "yuva444p10le", "-c:a", "pcm_s24le",
            str(src),
        ],
        check=True,
    )
    run = subprocess.run(
        [sys.executable, "-m", "cinemaster", "process", "-i", str(src), "-o", str(dst),
         "--preset", "aggressive", "--encoder-preset", "ultrafast"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    video, audio = _streams(dst)
    assert (video["width"], video["height"], video["pix_fmt"]) == (320, 240, "yuv420p")
    assert (audio["sample_rate"], audio["channels"]) == ("48000", 2)
