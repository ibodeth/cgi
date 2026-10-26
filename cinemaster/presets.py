"""Mastering configuration model and the built-in presets.

Every tunable of the pipeline lives on :class:`MasterConfig`. A preset is a
fully-populated ``MasterConfig``; CLI flags are applied on top of it as a
partial override (``None`` means "keep the preset value").
"""

from __future__ import annotations

import re
from fractions import Fraction
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

PresetName = Literal["subtle", "cinema", "aggressive"]
VideoCodec = Literal["libx264", "libx265"]
EncoderPreset = Literal[
    "ultrafast",
    "superfast",
    "veryfast",
    "faster",
    "fast",
    "medium",
    "slow",
    "slower",
    "veryslow",
    "placebo",
]

# Broadcast/NTSC style aliases that must map to exact rationals, never floats.
FPS_ALIASES: dict[str, Fraction] = {
    "23.976": Fraction(24000, 1001),
    "23.98": Fraction(24000, 1001),
    "29.97": Fraction(30000, 1001),
    "47.952": Fraction(48000, 1001),
    "59.94": Fraction(60000, 1001),
    "119.88": Fraction(120000, 1001),
}

_PSY_RD_RE = re.compile(r"^\d+(\.\d+)?,\d+(\.\d+)?$")
_DEBLOCK_RE = re.compile(r"^-?\d+,-?\d+$")
_BITRATE_RE = re.compile(r"^\d+[kKmM]?$")
_FOURCC_RE = re.compile(r"^[\x20-\x7e]{0,4}$")


def parse_fps(value: str) -> Fraction | None:
    """Parse a user FPS string. Returns ``None`` for ``"source"``.

    Accepts aliases (``29.97``), integers (``30``), decimals (``12.5``) and
    rationals (``30000/1001``).
    """
    text = value.strip().lower()
    if text == "source":
        return None
    if text in FPS_ALIASES:
        return FPS_ALIASES[text]
    try:
        fps = Fraction(text).limit_denominator(1001)
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError(f"invalid fps value: {value!r}") from exc
    if not (1 <= fps <= 240):
        raise ValueError(f"fps must be within 1..240, got {value!r}")
    return fps


class MasterConfig(BaseModel):
    """All parameters of the cine-master processing graph."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True, frozen=True)

    # --- 2. Spatial resampling -------------------------------------------------
    crop_pct: float = Field(1.4, ge=0.0, le=10.0, description="Total crop per axis, in % of the frame")
    crop_asymmetry: float = Field(
        0.15, ge=0.0, le=0.45, description="Max deviation of the left/right (top/bottom) split from 50/50"
    )
    crop_jitter: float = Field(0.06, ge=0.0, le=0.5, description="Relative random variation of the per-axis crop total")
    unsharp_amount: float = Field(0.15, ge=0.0, le=1.5, description="Luma unsharp-mask amount after the Lanczos rescale")

    # --- 3. De-banding, grain, calibration ------------------------------------
    deband_threshold: float = Field(0.015, ge=0.00003, le=0.5)
    deband_range: int = Field(16, ge=1, le=64)
    grain: int = Field(3, ge=1, le=10, description="Temporal luma grain strength (noise c0s)")
    chroma_grain: int = Field(1, ge=0, le=10, description="Temporal chroma grain strength (noise c1s/c2s)")
    eq_contrast: float = Field(1.015, ge=0.8, le=1.2)
    eq_brightness: float = Field(0.002, ge=-0.05, le=0.05)
    eq_saturation: float = Field(1.01, ge=0.8, le=1.2)
    eq_gamma: float = Field(0.99, ge=0.8, le=1.2)

    # --- 4. Temporal resampling & encoding -------------------------------------
    fps: str = Field("source", description='"source" or a target rate such as 24, 25, 29.97, 30, 30000/1001')
    timescale: int | None = Field(None, ge=1000, le=10_000_000, description="MP4/MOV video track timescale (None = auto)")
    codec: VideoCodec = "libx264"
    crf: int = Field(16, ge=0, le=51)
    encoder_preset: EncoderPreset = "slow"
    keyint: int | None = Field(None, ge=1, le=1000, description="GOP length (None = 2 x output fps)")
    min_keyint: int = Field(6, ge=1, le=500)
    aq_mode: int = Field(3, ge=0, le=4)
    aq_strength: float = Field(0.8, ge=0.0, le=3.0)
    psy_rd: str = "1.0,0.15"
    deblock: str = "-2,-2"

    # --- 1. Delivery clean-up ---------------------------------------------------
    strip_sei: bool = True
    faststart: bool = True
    vendor_id: str = Field("appl", description="4CC written to the MOV video sample entry vendor field")

    # --- 5. Audio ---------------------------------------------------------------
    audio_bitrate: str = "256k"
    audio_sample_rate: int = Field(48000, ge=8000, le=192000)
    audio_highpass: float = Field(22.0, ge=1.0, le=200.0)
    audio_lowpass: float = Field(19600.0, ge=4000.0, le=23900.0)
    audio_drift: float = Field(0.0006, ge=0.0, le=0.01, description="Micro-resample factor (asetrate x (1 + drift))")

    # --- Determinism -------------------------------------------------------------
    seed: int | None = Field(None, description="Fix the crop/grain randomisation (None = new variation every run)")

    @field_validator("fps")
    @classmethod
    def _validate_fps(cls, value: str) -> str:
        parse_fps(value)
        return value.strip().lower()

    @field_validator("psy_rd")
    @classmethod
    def _validate_psy_rd(cls, value: str) -> str:
        if not _PSY_RD_RE.match(value):
            raise ValueError('psy_rd must look like "1.0,0.15"')
        return value

    @field_validator("deblock")
    @classmethod
    def _validate_deblock(cls, value: str) -> str:
        if not _DEBLOCK_RE.match(value):
            raise ValueError('deblock must look like "-2,-2"')
        return value

    @field_validator("audio_bitrate")
    @classmethod
    def _validate_bitrate(cls, value: str) -> str:
        if not _BITRATE_RE.match(value):
            raise ValueError('audio_bitrate must look like "256k"')
        return value.lower()

    @field_validator("vendor_id")
    @classmethod
    def _validate_vendor(cls, value: str) -> str:
        if not _FOURCC_RE.match(value):
            raise ValueError("vendor_id must be at most 4 printable ASCII characters")
        return value

    @property
    def target_fps(self) -> Fraction | None:
        return parse_fps(self.fps)


PRESETS: dict[PresetName, MasterConfig] = {
    "subtle": MasterConfig(
        crop_pct=1.0,
        crop_asymmetry=0.12,
        unsharp_amount=0.10,
        deband_threshold=0.010,
        grain=2,
        chroma_grain=0,
        eq_contrast=1.008,
        eq_brightness=0.001,
        eq_saturation=1.005,
        eq_gamma=0.995,
        crf=16,
    ),
    "cinema": MasterConfig(),
    "aggressive": MasterConfig(
        crop_pct=2.2,
        crop_asymmetry=0.2,
        crop_jitter=0.1,
        unsharp_amount=0.22,
        deband_threshold=0.020,
        deband_range=20,
        grain=6,
        chroma_grain=2,
        eq_contrast=1.03,
        eq_brightness=0.004,
        eq_saturation=1.02,
        eq_gamma=0.98,
        crf=15,
        aq_strength=0.9,
    ),
}

PRESET_DESCRIPTIONS: dict[PresetName, str] = {
    "subtle": "Barely perceptible: light crop, fine luma-only grain, minimal grade.",
    "cinema": "Balanced 35mm look: the reference values of the pipeline.",
    "aggressive": "Heavy organic texture: stronger crop/grain/deband for very clean CGI.",
}


def resolve_config(preset: PresetName, overrides: dict[str, Any] | None = None) -> MasterConfig:
    """Return the preset with all non-``None`` overrides applied and re-validated."""
    base = PRESETS[preset]
    updates = {key: value for key, value in (overrides or {}).items() if value is not None}
    if not updates:
        return base
    # model_copy() skips validation, so rebuild through the constructor.
    return MasterConfig(**{**base.model_dump(), **updates})
