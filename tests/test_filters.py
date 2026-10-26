from __future__ import annotations

import random
from fractions import Fraction
from pathlib import Path

import pytest
from pydantic import ValidationError

from cinemaster.filters import (
    auto_timescale,
    build_audio_chain,
    build_bsf,
    build_plan,
    compute_crop,
    plan_color,
)
from cinemaster.presets import PRESETS, MasterConfig, parse_fps, resolve_config
from cinemaster.probe import MediaInfo, VideoStreamInfo, normalize_fps, AudioStreamInfo


def make_video(**overrides: object) -> VideoStreamInfo:
    values: dict[str, object] = dict(
        index=0,
        codec_name="h264",
        width=1920,
        height=1080,
        frame_width=1920,
        frame_height=1080,
        coded_width=1920,
        coded_height=1080,
        fps=Fraction(30),
        pix_fmt="yuv420p",
        rotation=0,
        sar=Fraction(1),
        color_space=None,
        color_primaries=None,
        color_transfer=None,
        color_range=None,
        nb_frames=None,
    )
    values.update(overrides)
    return VideoStreamInfo(**values)


def make_info(audio: bool = True, **video: object) -> MediaInfo:
    return MediaInfo(
        path=Path("/tmp/in.mp4"),
        format_name="mov,mp4",
        duration=10.0,
        video=make_video(**video),
        audio=AudioStreamInfo(index=1, codec_name="aac", sample_rate=44100, channels=2, channel_layout="stereo")
        if audio
        else None,
    )


@pytest.mark.parametrize("seed", range(200))
def test_crop_is_even_asymmetric_and_grid_shifted(seed: int) -> None:
    crop = compute_crop(1920, 1080, 1.5, 0.15, 0.06, random.Random(seed))
    for value in (crop.left, crop.right, crop.top, crop.bottom, crop.width, crop.height):
        assert value % 2 == 0
    assert crop.left != crop.right
    assert crop.top != crop.bottom
    assert crop.left % 8 != 0 and crop.top % 8 != 0
    assert crop.width == 1920 - crop.left - crop.right
    assert crop.height == 1080 - crop.top - crop.bottom
    # Total stays near 1.5% per axis (jitter 6% + even rounding).
    assert abs((crop.left + crop.right) - 28.8) <= 28.8 * 0.06 + 2
    assert abs((crop.top + crop.bottom) - 16.2) <= 16.2 * 0.06 + 2


def test_crop_zero_is_noop() -> None:
    crop = compute_crop(1280, 720, 0.0, 0.15, 0.06, random.Random(1))
    assert crop.is_noop and (crop.width, crop.height) == (1280, 720)


def test_crop_varies_between_runs_without_seed() -> None:
    crops = {compute_crop(3840, 2160, 1.4, 0.15, 0.06, random.Random()) for _ in range(20)}
    assert len(crops) > 1


def test_seed_makes_plan_reproducible() -> None:
    cfg = resolve_config("cinema", {"seed": 42})
    a = build_plan(make_info(), cfg, soxr=True)
    b = build_plan(make_info(), cfg, soxr=True)
    assert a.filter_complex == b.filter_complex


def test_video_chain_order_and_spec_values() -> None:
    plan = build_plan(make_info(), PRESETS["cinema"], soxr=True, rng=random.Random(3))
    video = plan.filter_complex.split(";")[0]
    assert video.startswith("[0:0]fps=fps=30,crop=")
    assert "flags=lanczos+accurate_rnd+full_chroma_int" in video
    assert "unsharp=lx=3:ly=3:la=0.15:cx=3:cy=3:ca=0.0" in video
    assert "deband=1thr=0.015:2thr=0.015:3thr=0.015:range=16:blur=true" in video
    assert "eq=contrast=1.015:brightness=0.002:saturation=1.01:gamma=0.99" in video
    assert ":c0s=3:c0f=t+u:c1s=1:c1f=t+u" in video
    order = [video.index(token) for token in ("fps=", "crop=", "scale=", "format=yuv420p", "unsharp=", "deband=", "eq=", "noise=")]
    assert order == sorted(order)
    assert video.endswith("[vout]")
    assert (plan.out_width, plan.out_height) == (1920, 1080)
    assert plan.keyint == 60 and plan.min_keyint == 6


def test_audio_chain_compensates_drift_exactly() -> None:
    chain = build_audio_chain(PRESETS["cinema"], soxr=False)
    assert "highpass=f=22,lowpass=f=19600" in chain
    assert "allpass=f=1000:width_type=h:width=200,allpass=f=4000:width_type=h:width=500" in chain
    assert "asetrate=48029" in chain
    tempo = float(chain.split("atempo=")[1].split(",")[0])
    assert abs(tempo * 48029 - 48000) < 1e-5
    assert chain.endswith("channel_layouts=stereo")


def test_no_audio_stream_means_no_audio_graph() -> None:
    plan = build_plan(make_info(audio=False), PRESETS["subtle"], soxr=True)
    assert plan.audio_label is None and ";" not in plan.filter_complex


def test_bsf_per_codec() -> None:
    assert build_bsf(PRESETS["cinema"]) == "filter_units=remove_types=6"
    assert build_bsf(resolve_config("cinema", {"codec": "libx265"})) == "filter_units=remove_types=39|40"
    assert build_bsf(resolve_config("cinema", {"strip_sei": False})) is None


def test_target_fps_overrides_source() -> None:
    plan = build_plan(make_info(), resolve_config("cinema", {"fps": "29.97"}), soxr=True)
    assert plan.fps == Fraction(30000, 1001)
    assert "fps=fps=30000/1001" in plan.filter_complex
    assert plan.keyint == 60
    assert plan.timescale == 30000


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (Fraction(30), Fraction(30)),
        (Fraction(2997, 100), Fraction(30000, 1001)),
        (Fraction(24000, 1001), Fraction(24000, 1001)),
        (Fraction(25), Fraction(25)),
        (Fraction(12), Fraction(12)),
    ],
)
def test_normalize_fps_picks_nearest(raw: Fraction, expected: Fraction) -> None:
    assert normalize_fps(raw) == expected


def test_parse_fps() -> None:
    assert parse_fps("source") is None
    assert parse_fps("29.97") == Fraction(30000, 1001)
    assert parse_fps("30") == Fraction(30)
    assert parse_fps("60000/1001") == Fraction(60000, 1001)
    with pytest.raises(ValueError):
        parse_fps("fast")


def test_auto_timescale_has_integer_frame_durations() -> None:
    for fps in (Fraction(24), Fraction(25), Fraction(30000, 1001), Fraction(24000, 1001), Fraction(60)):
        ts = auto_timescale(fps)
        assert ts >= 10_000
        assert (ts / fps).denominator == 1


def test_odd_and_rotated_sources() -> None:
    plan = build_plan(
        make_info(width=1081, height=1921, frame_width=1081, frame_height=1921), PRESETS["cinema"], soxr=True
    )
    assert (plan.out_width, plan.out_height) == (1080, 1920)
    assert plan.warnings


def test_color_planning() -> None:
    assert plan_color(make_video()).colorspace == "bt709"
    sd = plan_color(make_video(color_space="smpte170m"))
    assert (sd.colorspace, sd.sws_out_matrix) == ("smpte170m", "smpte170m")
    full = plan_color(make_video(pix_fmt="yuvj420p"))
    assert full.sws_in_range == "pc" and full.range == "tv"
    hdr = plan_color(make_video(color_transfer="smpte2084", color_space="bt2020nc", color_primaries="bt2020"))
    assert hdr.transfer == "smpte2084" and hdr.warnings


def test_overrides_are_validated() -> None:
    with pytest.raises(ValidationError):
        resolve_config("cinema", {"grain": 11})
    with pytest.raises(ValidationError):
        resolve_config("cinema", {"fps": "abc"})
    cfg = resolve_config("aggressive", {"crf": 18, "grain": None})
    assert cfg.crf == 18 and cfg.grain == PRESETS["aggressive"].grain


def test_defaults_match_spec() -> None:
    cfg = MasterConfig()
    assert (cfg.crop_pct, cfg.grain, cfg.crf, cfg.fps, cfg.strip_sei) == (1.4, 3, 16, "source", True)
    assert (cfg.aq_mode, cfg.aq_strength, cfg.psy_rd, cfg.deblock, cfg.min_keyint) == (3, 0.8, "1.0,0.15", "-2,-2", 6)
