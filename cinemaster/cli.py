"""Typer/Rich command-line interface for cine-master."""

from __future__ import annotations

import shlex
import signal
import threading
from enum import Enum
from pathlib import Path
from types import FrameType
from typing import Annotated, Any, Optional

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.markup import escape as esc
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TaskID,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table
from rich.text import Text

from . import __version__
from .engine import (
    REGISTRY,
    JobResult,
    MasteringError,
    discover_videos,
    master_file,
    output_for,
    run_batch,
)
from .filters import FilterPlan, fmt_rate
from .presets import PRESET_DESCRIPTIONS, PRESETS, MasterConfig, resolve_config
from .probe import MediaInfo, ProbeError, ToolMissingError, find_tool, probe

app = typer.Typer(
    name="cine-master",
    help="CGI/render delivery mastering: 35mm grain, de-banding and clean bitstreams via one FFmpeg graph.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
)
console = Console(highlight=False)
err_console = Console(stderr=True, highlight=False)


class Preset(str, Enum):
    subtle = "subtle"
    cinema = "cinema"
    aggressive = "aggressive"


class Codec(str, Enum):
    libx264 = "libx264"
    libx265 = "libx265"


class Container(str, Enum):
    mp4 = "mp4"
    mov = "mov"
    mkv = "mkv"
    keep = "keep"


TUNING = "Tuning overrides (default: preset value)"

PresetOpt = Annotated[Preset, typer.Option("--preset", "-p", help="Base preset.")]
CropOpt = Annotated[
    Optional[float],
    typer.Option("--crop-pct", min=0.0, max=10.0, help="Total crop per axis in % [cinema: 1.4].", rich_help_panel=TUNING),
]
GrainOpt = Annotated[
    Optional[int],
    typer.Option("--grain", min=1, max=10, help="Luma grain strength 1-10 [cinema: 3].", rich_help_panel=TUNING),
]
ChromaGrainOpt = Annotated[
    Optional[int],
    typer.Option("--chroma-grain", min=0, max=10, help="Chroma grain strength 0-10 [cinema: 1].", rich_help_panel=TUNING),
]
UnsharpOpt = Annotated[
    Optional[float],
    typer.Option("--unsharp", min=0.0, max=1.5, help="Luma unsharp amount after rescale [cinema: 0.15].", rich_help_panel=TUNING),
]
DebandOpt = Annotated[
    Optional[float],
    typer.Option("--deband", help="Deband threshold for all planes [cinema: 0.015].", rich_help_panel=TUNING),
]
CrfOpt = Annotated[
    Optional[int],
    typer.Option("--crf", min=0, max=51, help="Constant rate factor [default: 16].", rich_help_panel=TUNING),
]
FpsOpt = Annotated[
    Optional[str],
    typer.Option("--fps", help='Target frame rate: "source", 24, 25, 29.97, 30, 30000/1001 ...', rich_help_panel=TUNING),
]
CodecOpt = Annotated[Optional[Codec], typer.Option("--codec", help="Video encoder [default: libx264].", rich_help_panel=TUNING)]
EncPresetOpt = Annotated[
    Optional[str],
    typer.Option("--encoder-preset", help="x264/x265 speed preset [default: slow].", rich_help_panel=TUNING),
]
KeyintOpt = Annotated[
    Optional[int],
    typer.Option("--keyint", min=1, max=1000, help="GOP length [default: 2 x fps, i.e. 60 @ 30 fps].", rich_help_panel=TUNING),
]
SeiOpt = Annotated[
    Optional[bool],
    typer.Option("--strip-sei/--keep-sei", help="Remove SEI NAL units from the bitstream [default: strip].", rich_help_panel=TUNING),
]
AudioBitrateOpt = Annotated[
    Optional[str], typer.Option("--audio-bitrate", help="AAC bitrate [default: 256k].", rich_help_panel=TUNING)
]
VendorOpt = Annotated[
    Optional[str],
    typer.Option("--vendor", help="MOV sample-entry vendor 4CC; '' for zero [default: appl].", rich_help_panel=TUNING),
]
TimescaleOpt = Annotated[
    Optional[int], typer.Option("--timescale", help="MP4/MOV video track timescale [default: auto].", rich_help_panel=TUNING)
]
SeedOpt = Annotated[
    Optional[int],
    typer.Option("--seed", help="Make crop/grain randomisation reproducible [default: new variation per run].", rich_help_panel=TUNING),
]
OverwriteOpt = Annotated[bool, typer.Option("--overwrite", "-y", help="Replace existing output files.")]
DryRunOpt = Annotated[bool, typer.Option("--dry-run", help="Print the FFmpeg command(s) without encoding.")]
VerboseOpt = Annotated[bool, typer.Option("--verbose", "-v", help="Show the full FFmpeg command.")]


def _build_config(preset: Preset, **overrides: Any) -> MasterConfig:
    mapping = {
        "crop_pct": overrides.get("crop_pct"),
        "grain": overrides.get("grain"),
        "chroma_grain": overrides.get("chroma_grain"),
        "unsharp_amount": overrides.get("unsharp"),
        "deband_threshold": overrides.get("deband"),
        "crf": overrides.get("crf"),
        "fps": overrides.get("fps"),
        "codec": overrides["codec"].value if overrides.get("codec") else None,
        "encoder_preset": overrides.get("encoder_preset"),
        "keyint": overrides.get("keyint"),
        "strip_sei": overrides.get("strip_sei"),
        "audio_bitrate": overrides.get("audio_bitrate"),
        "vendor_id": overrides.get("vendor"),
        "timescale": overrides.get("timescale"),
        "seed": overrides.get("seed"),
    }
    try:
        return resolve_config(preset.value, mapping)
    except ValidationError as exc:
        for error in exc.errors():
            field = ".".join(str(part) for part in error["loc"])
            err_console.print(f"[red]Invalid option[/] [bold]{field}[/]: {esc(error['msg'])}")
        raise typer.Exit(2) from None


def _check_ffmpeg() -> None:
    try:
        find_tool("ffmpeg")
        find_tool("ffprobe")
    except ToolMissingError as exc:
        err_console.print(f"[red]{esc(str(exc))}[/]")
        raise typer.Exit(127) from None


def _plan_table(info: MediaInfo, plan: FilterPlan, cfg: MasterConfig) -> Table:
    v = info.video
    table = Table.grid(padding=(0, 2))
    table.add_column(style="cyan", no_wrap=True)
    table.add_column()
    table.add_row("Source", f"{v.width}x{v.height} @ {float(v.fps):.3f} fps · {v.codec_name} · {v.pix_fmt} · {info.duration:.2f}s")
    table.add_row("Crop", plan.crop.describe(v.frame_width, v.frame_height))
    table.add_row("Rescale", f"lanczos -> {plan.out_width}x{plan.out_height} · unsharp la={cfg.unsharp_amount}")
    table.add_row("Texture", f"deband {cfg.deband_threshold} · grain Y={cfg.grain} C={cfg.chroma_grain} (seed {plan.grain_seed})")
    table.add_row(
        "Encode",
        f"{cfg.codec} crf {cfg.crf} {cfg.encoder_preset}/grain · {fmt_rate(plan.fps)} fps · "
        f"keyint {plan.keyint} · timescale {plan.timescale}",
    )
    table.add_row("Colour", f"{plan.color.colorspace}/{plan.color.primaries}/{plan.color.transfer} · {plan.color.range} range")
    if info.audio is not None:
        a = info.audio
        table.add_row(
            "Audio",
            f"{a.codec_name} {a.sample_rate} Hz {a.channels}ch -> AAC {cfg.audio_sample_rate} Hz stereo {cfg.audio_bitrate}",
        )
    else:
        table.add_row("Audio", "none (video only)")
    table.add_row("Bitstream", plan.bsf or "SEI kept")
    return table


def _print_warnings(plan: FilterPlan, prefix: str = "") -> None:
    for warning in plan.warnings:
        err_console.print(f"[yellow]warning[/] {esc(prefix + warning)}")


def _print_command(cmd: list[str] | tuple[str, ...]) -> None:
    console.print(Panel(Text(shlex.join(cmd)), title="ffmpeg", border_style="dim", expand=False))


def _raise_interrupt(signum: int, frame: FrameType | None) -> None:
    raise KeyboardInterrupt


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"cine-master {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool, typer.Option("--version", callback=_version_callback, is_eager=True, help="Show the version and exit.")
    ] = False,
) -> None:
    """cine-master: organic 35mm finishing and clean social-media delivery."""
    # Treat SIGTERM like Ctrl+C so FFmpeg workers stop and partial files are removed.
    signal.signal(signal.SIGTERM, _raise_interrupt)


@app.command()
def process(
    input: Annotated[
        Path, typer.Option("--input", "-i", exists=True, dir_okay=False, readable=True, help="Source video.")
    ],
    output: Annotated[Path, typer.Option("--output", "-o", help="Destination (.mp4, .mov, .mkv).")],
    preset: PresetOpt = Preset.cinema,
    crop_pct: CropOpt = None,
    grain: GrainOpt = None,
    chroma_grain: ChromaGrainOpt = None,
    unsharp: UnsharpOpt = None,
    deband: DebandOpt = None,
    crf: CrfOpt = None,
    fps: FpsOpt = None,
    codec: CodecOpt = None,
    encoder_preset: EncPresetOpt = None,
    keyint: KeyintOpt = None,
    strip_sei: SeiOpt = None,
    audio_bitrate: AudioBitrateOpt = None,
    vendor: VendorOpt = None,
    timescale: TimescaleOpt = None,
    seed: SeedOpt = None,
    overwrite: OverwriteOpt = False,
    dry_run: DryRunOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """Master a single file."""
    _check_ffmpeg()
    cfg = _build_config(
        preset,
        crop_pct=crop_pct,
        grain=grain,
        chroma_grain=chroma_grain,
        unsharp=unsharp,
        deband=deband,
        crf=crf,
        fps=fps,
        codec=codec,
        encoder_preset=encoder_preset,
        keyint=keyint,
        strip_sei=strip_sei,
        audio_bitrate=audio_bitrate,
        vendor=vendor,
        timescale=timescale,
        seed=seed,
    )

    progress = Progress(
        TextColumn("[bold]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
        transient=False,
    )
    task_id: TaskID | None = None

    def on_start(info: MediaInfo, plan: FilterPlan, cmd: list[str]) -> None:
        nonlocal task_id
        console.print(Panel(_plan_table(info, plan, cfg), title=f"[bold]{esc(info.path.name)}[/] · preset {preset.value}", expand=False))
        _print_warnings(plan)
        if verbose or dry_run:
            _print_command(cmd)
        if not dry_run:
            progress.start()
            task_id = progress.add_task("mastering", total=max(info.duration, 0.001))

    def on_progress(seconds: float) -> None:
        if task_id is not None:
            progress.update(task_id, completed=seconds)

    try:
        result = master_file(
            input, output, cfg, overwrite=overwrite, dry_run=dry_run, on_start=on_start, on_progress=on_progress
        )
    except KeyboardInterrupt:
        REGISTRY.terminate_all()
        if task_id is not None:
            progress.stop()
        err_console.print("[red]Interrupted.[/]")
        raise typer.Exit(130) from None
    if task_id is not None:
        if result.ok:
            progress.update(task_id, completed=progress.tasks[0].total)
        progress.stop()

    if result.skipped:
        err_console.print(f"[yellow]skipped[/] {esc(str(result.output))}: {esc(result.message)}")
        raise typer.Exit(1)
    if not result.ok:
        err_console.print(Panel(Text(result.message), title="[red]Failed[/]", border_style="red"))
        if result.command and not verbose:
            _print_command(result.command)
        raise typer.Exit(1)
    if not dry_run:
        console.print(f"[green]✓[/] {esc(str(result.output))} [dim]({result.elapsed:.1f}s)[/]")


@app.command()
def batch(
    directory: Annotated[
        Path, typer.Option("--dir", "-d", exists=True, file_okay=False, help="Folder with .mp4/.mov/.mkv renders.")
    ],
    out_dir: Annotated[Path, typer.Option("--out-dir", "-o", help="Destination folder.")],
    preset: PresetOpt = Preset.cinema,
    workers: Annotated[int, typer.Option("--workers", "-w", min=1, max=32, help="Parallel FFmpeg processes.")] = 1,
    recursive: Annotated[bool, typer.Option("--recursive", "-r", help="Include sub-folders.")] = False,
    container: Annotated[Container, typer.Option("--container", help="Output container.")] = Container.mp4,
    suffix: Annotated[str, typer.Option("--suffix", help="Appended to output file stems, e.g. _master.")] = "",
    crop_pct: CropOpt = None,
    grain: GrainOpt = None,
    chroma_grain: ChromaGrainOpt = None,
    unsharp: UnsharpOpt = None,
    deband: DebandOpt = None,
    crf: CrfOpt = None,
    fps: FpsOpt = None,
    codec: CodecOpt = None,
    encoder_preset: EncPresetOpt = None,
    keyint: KeyintOpt = None,
    strip_sei: SeiOpt = None,
    audio_bitrate: AudioBitrateOpt = None,
    vendor: VendorOpt = None,
    timescale: TimescaleOpt = None,
    seed: SeedOpt = None,
    overwrite: OverwriteOpt = False,
    dry_run: DryRunOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """Master every video in a folder, in parallel."""
    _check_ffmpeg()
    cfg = _build_config(
        preset,
        crop_pct=crop_pct,
        grain=grain,
        chroma_grain=chroma_grain,
        unsharp=unsharp,
        deband=deband,
        crf=crf,
        fps=fps,
        codec=codec,
        encoder_preset=encoder_preset,
        keyint=keyint,
        strip_sei=strip_sei,
        audio_bitrate=audio_bitrate,
        vendor=vendor,
        timescale=timescale,
        seed=seed,
    )
    try:
        sources = discover_videos(directory, recursive, exclude=out_dir)
    except MasteringError as exc:
        err_console.print(f"[red]{esc(str(exc))}[/]")
        raise typer.Exit(2) from None
    if not sources:
        err_console.print(f"[yellow]No .mp4/.mov/.mkv files found in {esc(str(directory))}[/]")
        raise typer.Exit(1)

    jobs = [(src, output_for(src, directory, out_dir, container.value, suffix)) for src in sources]
    console.print(
        f"[bold]{len(jobs)}[/] file(s) · preset [cyan]{preset.value}[/] · {workers} worker(s) · "
        f"{cfg.codec} crf {cfg.crf} · -> {esc(str(out_dir))}"
    )

    progress = Progress(
        TextColumn("{task.description}", markup=False),
        BarColumn(),
        TaskProgressColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
    )
    lock = threading.Lock()
    overall = progress.add_task("total", total=len(jobs))
    file_tasks: dict[Path, TaskID] = {}

    def on_start(src: Path, info: MediaInfo, plan: FilterPlan, cmd: list[str]) -> None:
        with lock:
            prefix = f"{src.name}: "
            _print_warnings(plan, prefix)
            if verbose or dry_run:
                progress.console.print(f"[cyan]{esc(src.name)}[/] crop {plan.crop.describe(info.video.frame_width, info.video.frame_height)}")
                progress.console.print(Panel(Text(shlex.join(cmd)), title=Text(src.name), border_style="dim", expand=False))
            if not dry_run:
                file_tasks[src.resolve()] = progress.add_task(src.name, total=max(info.duration, 0.001))

    def on_progress(src: Path, seconds: float) -> None:
        task = file_tasks.get(src.resolve())
        if task is not None:
            progress.update(task, completed=seconds)

    def on_finish(result: JobResult) -> None:
        with lock:
            task = file_tasks.pop(result.input, None)
            if task is not None:
                progress.remove_task(task)
            progress.advance(overall)
            if result.ok and not dry_run:
                progress.console.print(f"[green]✓[/] {esc(result.input.name)} -> {esc(str(result.output))} [dim]({result.elapsed:.1f}s)[/]")
            elif result.skipped:
                progress.console.print(f"[yellow]•[/] {esc(result.input.name)}: {esc(result.message)}")
            elif not result.ok:
                last_line = result.message.splitlines()[-1] if result.message else "failed"
                progress.console.print(f"[red]✗[/] {esc(result.input.name)}: {esc(last_line)}")

    try:
        with progress:
            results = run_batch(
                jobs,
                cfg,
                workers=workers,
                overwrite=overwrite,
                dry_run=dry_run,
                on_start=on_start,
                on_progress=on_progress,
                on_finish=on_finish,
            )
    except KeyboardInterrupt:
        err_console.print("[red]Interrupted, all FFmpeg workers stopped.[/]")
        raise typer.Exit(130) from None

    failed = [r for r in results if not r.ok and not r.skipped]
    skipped = [r for r in results if r.skipped]
    done = [r for r in results if r.ok]
    console.print(f"[green]{len(done)} ok[/] · [yellow]{len(skipped)} skipped[/] · [red]{len(failed)} failed[/]")
    for result in failed:
        err_console.print(Panel(Text(result.message), title=Text(result.input.name, style="red"), border_style="red"))
    if failed:
        raise typer.Exit(1)


@app.command("probe")
def probe_cmd(
    file: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True, help="Video to analyse.")],
) -> None:
    """Show what cine-master detects in a file."""
    _check_ffmpeg()
    try:
        info = probe(file)
    except ProbeError as exc:
        err_console.print(f"[red]{esc(str(exc))}[/]")
        raise typer.Exit(1) from None
    v = info.video
    table = Table(show_header=False, box=None)
    table.add_column(style="cyan")
    table.add_column()
    table.add_row("Container", info.format_name)
    table.add_row("Duration", f"{info.duration:.3f} s")
    table.add_row("Video", f"#{v.index} {v.codec_name} {v.pix_fmt}")
    table.add_row("Display size", f"{v.width}x{v.height} (coded {v.coded_width}x{v.coded_height}, SAR {v.sar}, rotation {v.rotation}°)")
    table.add_row("Frame rate", f"{fmt_rate(v.fps)} ({float(v.fps):.3f} fps)")
    table.add_row(
        "Colour",
        f"matrix={v.color_space or '?'} primaries={v.color_primaries or '?'} "
        f"transfer={v.color_transfer or '?'} range={v.color_range or '?'}",
    )
    if info.audio is not None:
        a = info.audio
        table.add_row("Audio", f"#{a.index} {a.codec_name} {a.sample_rate} Hz {a.channels}ch {a.channel_layout or ''}")
    else:
        table.add_row("Audio", "none")
    console.print(Panel(table, title=Text(file.name), expand=False))


@app.command("presets")
def presets_cmd() -> None:
    """List the built-in presets and their values."""
    fields = [
        "crop_pct",
        "crop_asymmetry",
        "unsharp_amount",
        "deband_threshold",
        "grain",
        "chroma_grain",
        "eq_contrast",
        "eq_brightness",
        "eq_saturation",
        "eq_gamma",
        "crf",
        "aq_strength",
    ]
    table = Table(title="cine-master presets")
    table.add_column("parameter", style="cyan")
    for name in PRESETS:
        table.add_column(name, justify="right")
    for field in fields:
        table.add_row(field, *(str(getattr(cfg, field)) for cfg in PRESETS.values()))
    console.print(table)
    for name, description in PRESET_DESCRIPTIONS.items():
        console.print(f"[bold]{name}[/]: {description}")


if __name__ == "__main__":
    app()
