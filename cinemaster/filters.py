"""Builders for the FFmpeg ``filter_complex`` graph and the bitstream filters.

Everything here is pure (no subprocess calls) so the generated graph can be
unit tested and printed with ``--dry-run``.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from fractions import Fraction

from .presets import MasterConfig
from .probe import MediaInfo, VideoStreamInfo

VIDEO_LABEL = "vout"
AUDIO_LABEL = "aout"

# ffprobe colour-matrix names -> swscale (scale filter) matrix names.
_SWS_MATRIX: dict[str, str] = {
    "bt709": "bt709",
    "smpte170m": "smpte170m",
    "bt470bg": "bt470",
    "bt2020nc": "bt2020",
    "bt2020c": "bt2020",
    "fcc": "fcc",
    "smpte240m": "smpte240m",
}
_SD_MATRICES = {"smpte170m", "bt470bg"}
_SDR_TRANSFERS = {"bt709", "smpte170m", "bt470bg", "bt470m", "bt2020-10", "bt2020-12"}

# H.264 SEI = NAL type 6. HEVC prefix/suffix SEI = NAL types 39/40.
_SEI_NAL_TYPES: dict[str, str] = {"libx264": "6", "libx265": "39|40"}


def fmt_num(value: float, digits: int = 6) -> str:
    """Format a float for FFmpeg option strings (no exponent, no trailing zeros)."""
    text = f"{value:.{digits}f}".rstrip("0").rstrip(".")
    return text if text not in {"", "-0"} else "0"


def fmt_rate(rate: Fraction) -> str:
    return str(rate.numerator) if rate.denominator == 1 else f"{rate.numerator}/{rate.denominator}"


def _even(value: float) -> int:
    return int(2 * round(value / 2))


def _even_floor(value: int) -> int:
    return max(2, value - (value % 2))


@dataclass(frozen=True)
class CropWindow:
    """Pixels removed from each edge, and the size of the remaining window."""

    left: int
    right: int
    top: int
    bottom: int
    width: int
    height: int

    @property
    def is_noop(self) -> bool:
        return self.left == self.right == self.top == self.bottom == 0

    def describe(self, src_w: int, src_h: int) -> str:
        def pct(px: int, size: int) -> str:
            return f"{100 * px / size:.2f}%"

        return (
            f"L {self.left}px ({pct(self.left, src_w)}) · R {self.right}px ({pct(self.right, src_w)}) · "
            f"T {self.top}px ({pct(self.top, src_h)}) · B {self.bottom}px ({pct(self.bottom, src_h)})"
        )


def _split_axis(size: int, pct: float, asymmetry: float, jitter: float, rng: random.Random) -> tuple[int, int]:
    """Split a crop budget for one axis into two even, asymmetric edge offsets."""
    budget = size * pct / 100.0 * (1.0 + rng.uniform(-jitter, jitter))
    total = min(_even(budget), max(0, _even_floor(size) - 16))
    if total < 2:
        return 0, 0
    # Guarantee a visible asymmetry: deviate at least ~1/3 of the allowed range.
    deviation = rng.uniform(asymmetry * 0.35, asymmetry) * rng.choice((-1.0, 1.0))
    lead = min(max(_even(total * (0.5 + deviation)), 0), total)
    if total >= 4:
        # Even rounding can collapse small budgets back to a symmetric split.
        if lead == total - lead:
            lead += 2 if deviation >= 0 else -2
        # The leading offset decides where the 8x8/16x16 block grid lands. A
        # multiple of 8 would keep the old grid intact, so move it one
        # chroma-safe step further away from the centre.
        if lead % 8 == 0:
            outward = lead == 0 or (lead > total - lead and lead + 2 <= total)
            lead += 2 if outward else -2
    return lead, total - lead


def compute_crop(
    width: int,
    height: int,
    pct: float,
    asymmetry: float,
    jitter: float,
    rng: random.Random,
) -> CropWindow:
    """Asymmetric micro-crop with even offsets (4:2:0 safe) and shifted block grid."""
    left, right = _split_axis(width, pct, asymmetry, jitter, rng)
    top, bottom = _split_axis(height, pct, asymmetry, jitter, rng)
    return CropWindow(
        left=left,
        right=right,
        top=top,
        bottom=bottom,
        width=_even_floor(width - left - right),
        height=_even_floor(height - top - bottom),
    )


@dataclass(frozen=True)
class ColorPlan:
    """Scaler conversion settings plus the VUI tags written to the encoded stream."""

    sws_in_matrix: str
    sws_out_matrix: str
    sws_in_range: str
    colorspace: str
    primaries: str
    transfer: str
    range: str = "tv"
    warnings: tuple[str, ...] = ()


def plan_color(video: VideoStreamInfo) -> ColorPlan:
    """Decide matrix/range conversion and output tags.

    Delivery target is limited-range SDR. HD/RGB/untagged sources become
    BT.709; explicitly tagged BT.601 sources keep BT.601; HDR is passed through
    with its tags (no tone mapping) and a warning.
    """
    src_matrix = video.color_space
    sws_in_matrix = _SWS_MATRIX.get(src_matrix or "", "auto")
    if video.color_range == "pc" or video.pix_fmt.startswith("yuvj"):
        sws_in_range = "pc"
    elif video.color_range == "tv":
        sws_in_range = "tv"
    else:
        sws_in_range = "auto"

    warnings: list[str] = []
    if video.is_hdr:
        warnings.append(
            f"HDR transfer '{video.color_transfer}' detected: tags are preserved but no tone mapping "
            "is applied and the output is 8-bit; grade to SDR before mastering for social delivery."
        )
        matrix = src_matrix if src_matrix in _SWS_MATRIX else "bt2020nc"
        return ColorPlan(
            sws_in_matrix=sws_in_matrix,
            sws_out_matrix=_SWS_MATRIX[matrix],
            sws_in_range=sws_in_range,
            colorspace=matrix,
            primaries=video.color_primaries or "bt2020",
            transfer=video.color_transfer or "smpte2084",
            warnings=tuple(warnings),
        )

    if not video.is_rgb and src_matrix in _SD_MATRICES:
        default_primaries = "smpte170m" if src_matrix == "smpte170m" else "bt470bg"
        transfer = video.color_transfer if video.color_transfer in _SDR_TRANSFERS else "smpte170m"
        return ColorPlan(
            sws_in_matrix=sws_in_matrix,
            sws_out_matrix=_SWS_MATRIX[src_matrix],
            sws_in_range=sws_in_range,
            colorspace=src_matrix,
            primaries=video.color_primaries or default_primaries,
            transfer=transfer,
        )

    if video.color_transfer == "iec61966-2-1":
        warnings.append("sRGB transfer tag replaced by BT.709 for platform-consistent playback.")
    return ColorPlan(
        sws_in_matrix=sws_in_matrix,
        sws_out_matrix="bt709",
        sws_in_range=sws_in_range,
        colorspace="bt709",
        primaries="bt709",
        transfer="bt709",
        warnings=tuple(warnings),
    )


def auto_timescale(fps: Fraction) -> int:
    """Track timescale in which every frame duration is an exact integer tick count."""
    timescale = fps.numerator
    while timescale < 10_000:
        timescale *= 2
    return timescale


@dataclass(frozen=True)
class FilterPlan:
    """Everything derived from probe + config that the command builder needs."""

    video_graph: str
    audio_graph: str | None
    video_label: str
    audio_label: str | None
    out_width: int
    out_height: int
    fps: Fraction
    keyint: int
    min_keyint: int
    timescale: int
    crop: CropWindow
    color: ColorPlan
    grain_seed: int
    bsf: str | None
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def filter_complex(self) -> str:
        """Both branches as one readable graph description (for display and tests)."""
        return ";".join(g for g in (self.video_graph, self.audio_graph) if g)


def build_video_chain(
    cfg: MasterConfig,
    crop: CropWindow,
    fps: Fraction,
    color: ColorPlan,
    out_w: int,
    out_h: int,
    grain_seed: int,
) -> str:
    steps: list[str] = [f"fps=fps={fmt_rate(fps)}"]

    # 2. Asymmetric micro-crop, then Lanczos back to the delivery resolution.
    if not crop.is_noop:
        steps.append(f"crop={crop.width}:{crop.height}:{crop.left}:{crop.top}")
    steps.append(
        f"scale={out_w}:{out_h}"
        ":flags=lanczos+accurate_rnd+full_chroma_int"
        f":in_range={color.sws_in_range}:out_range={color.range}"
        f":in_color_matrix={color.sws_in_matrix}:out_color_matrix={color.sws_out_matrix}"
    )
    # Explicit 4:2:0 conversion inside the Lanczos scaler, so FFmpeg never
    # auto-inserts a default (bicubic) converter further down the graph.
    steps.append("format=yuv420p")
    steps.append("setsar=1")
    if cfg.unsharp_amount > 0:
        steps.append(f"unsharp=lx=3:ly=3:la={fmt_num(cfg.unsharp_amount)}:cx=3:cy=3:ca=0.0")

    # 3. De-band first, then the grade, then grain as the final dithering layer
    #    so the grade's 8-bit re-quantisation is broken up by the grain.
    thr = fmt_num(cfg.deband_threshold)
    steps.append(f"deband=1thr={thr}:2thr={thr}:3thr={thr}:range={cfg.deband_range}:blur=true")
    steps.append(
        "eq="
        f"contrast={fmt_num(cfg.eq_contrast)}"
        f":brightness={fmt_num(cfg.eq_brightness)}"
        f":saturation={fmt_num(cfg.eq_saturation)}"
        f":gamma={fmt_num(cfg.eq_gamma)}"
    )
    noise = f"noise=all_seed={grain_seed}:c0s={cfg.grain}:c0f=t+u"
    if cfg.chroma_grain > 0:
        noise += f":c1s={cfg.chroma_grain}:c1f=t+u:c2s={cfg.chroma_grain}:c2f=t+u"
    steps.append(noise)
    return ",".join(steps)


def build_audio_chain(cfg: MasterConfig, soxr: bool) -> str:
    rate = cfg.audio_sample_rate
    resampler = ":resampler=soxr:precision=28" if soxr else ":filter_size=64:phase_shift=10:cutoff=0.97"
    steps: list[str] = [
        f"aresample={rate}{resampler}",
        f"highpass=f={fmt_num(cfg.audio_highpass)}",
        f"lowpass=f={fmt_num(cfg.audio_lowpass)}",
        "allpass=f=1000:width_type=h:width=200",
        "allpass=f=4000:width_type=h:width=500",
    ]
    shifted_rate = round(rate * (1.0 + cfg.audio_drift))
    if shifted_rate != rate:
        # asetrate only takes integers, so the compensating tempo is derived from
        # the rounded rate: the output duration stays sample-exact.
        steps += [
            f"asetrate={shifted_rate}",
            f"aresample={rate}{resampler}",
            f"atempo={fmt_num(rate / shifted_rate, 10)}",
        ]
    steps.append(f"aformat=sample_fmts=fltp:sample_rates={rate}:channel_layouts=stereo")
    return ",".join(steps)


def build_bsf(cfg: MasterConfig) -> str | None:
    if not cfg.strip_sei:
        return None
    return f"filter_units=remove_types={_SEI_NAL_TYPES[cfg.codec]}"


def build_plan(info: MediaInfo, cfg: MasterConfig, soxr: bool, rng: random.Random | None = None) -> FilterPlan:
    """Assemble the full filter graph for ``info`` under ``cfg``."""
    rng = rng or (random.Random(cfg.seed) if cfg.seed is not None else random.Random())
    video = info.video

    crop = compute_crop(
        video.frame_width,
        video.frame_height,
        cfg.crop_pct,
        cfg.crop_asymmetry,
        cfg.crop_jitter,
        rng,
    )
    out_w, out_h = _even_floor(video.width), _even_floor(video.height)
    fps = cfg.target_fps or video.fps
    color = plan_color(video)
    grain_seed = rng.randrange(0, 2**31 - 1)

    video_graph = f"[0:{video.index}]{build_video_chain(cfg, crop, fps, color, out_w, out_h, grain_seed)}[{VIDEO_LABEL}]"
    audio_graph: str | None = None
    audio_label: str | None = None
    if info.audio is not None:
        audio_graph = f"[0:{info.audio.index}]{build_audio_chain(cfg, soxr)}[{AUDIO_LABEL}]"
        audio_label = AUDIO_LABEL

    keyint = cfg.keyint if cfg.keyint is not None else max(1, round(2 * fps))
    warnings = list(color.warnings)
    if (out_w, out_h) != (video.width, video.height):
        warnings.append(f"odd source size {video.width}x{video.height} rounded to {out_w}x{out_h} for 4:2:0.")

    return FilterPlan(
        video_graph=video_graph,
        audio_graph=audio_graph,
        video_label=VIDEO_LABEL,
        audio_label=audio_label,
        out_width=out_w,
        out_height=out_h,
        fps=fps,
        keyint=keyint,
        min_keyint=min(cfg.min_keyint, keyint),
        timescale=cfg.timescale or auto_timescale(fps),
        crop=crop,
        color=color,
        grain_seed=grain_seed,
        bsf=build_bsf(cfg),
        warnings=tuple(warnings),
    )
