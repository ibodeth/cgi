"""FFmpeg command assembly, subprocess supervision and batch execution."""

from __future__ import annotations

import os
import random
import subprocess
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from .container import ContainerPatchError, sanitize_sample_entries
from .filters import FilterPlan, build_plan, fmt_num
from .presets import MasterConfig
from .probe import MediaInfo, ProbeError, ToolMissingError, find_tool, has_soxr, probe, require_encoder

VIDEO_EXTENSIONS: frozenset[str] = frozenset({".mp4", ".mov", ".mkv"})
OUTPUT_MUXERS: dict[str, str] = {".mp4": "mp4", ".m4v": "mp4", ".mov": "mov", ".mkv": "matroska"}
_ISOBMFF_MUXERS = {"mp4", "mov"}

ProgressCallback = Callable[[float], None]
"""Receives the number of output seconds encoded so far."""


class MasteringError(RuntimeError):
    """Raised when FFmpeg fails or an input/output path is unusable."""


@dataclass(frozen=True)
class JobResult:
    input: Path
    output: Path
    ok: bool
    skipped: bool = False
    message: str = ""
    elapsed: float = 0.0
    command: tuple[str, ...] = ()
    plan: FilterPlan | None = None
    info: MediaInfo | None = None


def muxer_for(path: Path) -> str:
    try:
        return OUTPUT_MUXERS[path.suffix.lower()]
    except KeyError:
        supported = ", ".join(sorted(OUTPUT_MUXERS))
        raise MasteringError(f"unsupported output extension '{path.suffix}' (use one of: {supported})") from None


def _video_codec_args(cfg: MasterConfig, plan: FilterPlan, muxer: str) -> list[str]:
    common = [
        "-c:v",
        cfg.codec,
        "-preset",
        cfg.encoder_preset,
        "-tune",
        "grain",
        "-crf",
        str(cfg.crf),
        "-pix_fmt",
        "yuv420p",
    ]
    if cfg.codec == "libx264":
        params = (
            f"aq-mode={cfg.aq_mode}:aq-strength={fmt_num(cfg.aq_strength)}"
            f":psy-rd={cfg.psy_rd}:deblock={cfg.deblock}"
            f":keyint={plan.keyint}:min-keyint={plan.min_keyint}"
        )
        return common + ["-profile:v", "high", "-x264-params", params]

    psy_rd = cfg.psy_rd.split(",")[0]
    params = (
        f"log-level=error:info=0:aq-mode={min(cfg.aq_mode, 4)}:aq-strength={fmt_num(cfg.aq_strength)}"
        f":psy-rd={psy_rd}:deblock={cfg.deblock}"
        f":keyint={plan.keyint}:min-keyint={plan.min_keyint}"
    )
    args = common + ["-profile:v", "main", "-x265-params", params]
    if muxer in _ISOBMFF_MUXERS:
        # hvc1 (parameter sets in the sample entry) is what Apple/Instagram expect.
        args += ["-tag:v", "hvc1"]
    return args


def build_command(
    info: MediaInfo,
    cfg: MasterConfig,
    plan: FilterPlan,
    output: Path,
    muxer: str,
) -> list[str]:
    """Return the complete ffmpeg argv for one file."""
    cmd: list[str] = [
        find_tool("ffmpeg"),
        "-hide_banner",
        "-nostdin",
        "-y",
        "-loglevel",
        "error",
        "-nostats",
        "-progress",
        "pipe:1",
        "-i",
        str(info.path),
        "-filter_complex",
        plan.video_graph,
    ]
    # Video and audio are two independent complex graphs in the same process.
    # Joining them into one multi-input graph can stall FFmpeg (seen on 6.1
    # with interleaved ProRes/PCM MOV sources) while one branch waits for
    # packets the demuxer is holding back for the other.
    if plan.audio_graph:
        cmd += ["-filter_complex", plan.audio_graph]
    cmd += ["-map", f"[{plan.video_label}]"]
    if plan.audio_label:
        cmd += ["-map", f"[{plan.audio_label}]"]

    # 1. Delivery clean-up: no global/stream/chapter/program metadata, no
    #    version strings, no encoder SEI.
    cmd += [
        "-map_metadata",
        "-1",
        "-map_metadata:s",
        "-1",
        "-map_chapters",
        "-1",
        "-fflags",
        "+bitexact",
        "-flags:v",
        "+bitexact",
        "-flags:a",
        "+bitexact",
        "-sn",
        "-dn",
    ]

    # 4. Encoding.
    cmd += _video_codec_args(cfg, plan, muxer)
    cmd += [
        "-colorspace",
        plan.color.colorspace,
        "-color_primaries",
        plan.color.primaries,
        "-color_trc",
        plan.color.transfer,
        "-color_range",
        plan.color.range,
    ]
    if plan.bsf:
        cmd += ["-bsf:v", plan.bsf]

    # 5. Audio.
    if plan.audio_label:
        cmd += [
            "-c:a",
            "aac",
            "-profile:a",
            "aac_low",
            "-b:a",
            cfg.audio_bitrate,
            "-ar",
            str(cfg.audio_sample_rate),
            "-ac",
            "2",
        ]
    else:
        cmd += ["-an"]

    if muxer in _ISOBMFF_MUXERS:
        if cfg.faststart:
            cmd += ["-movflags", "+faststart"]
        cmd += ["-video_track_timescale", str(plan.timescale)]
    cmd += ["-max_muxing_queue_size", "4096", "-f", muxer, str(output)]
    return cmd


class _ProcessRegistry:
    """Tracks running ffmpeg processes so Ctrl+C can stop every worker."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._procs: set[subprocess.Popen[str]] = set()
        self.cancelled = threading.Event()

    def add(self, proc: subprocess.Popen[str]) -> None:
        with self._lock:
            self._procs.add(proc)

    def discard(self, proc: subprocess.Popen[str]) -> None:
        with self._lock:
            self._procs.discard(proc)

    def terminate_all(self) -> None:
        self.cancelled.set()
        with self._lock:
            procs = list(self._procs)
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()
        deadline = time.monotonic() + 5
        for proc in procs:
            try:
                proc.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                proc.kill()


REGISTRY = _ProcessRegistry()


def run_ffmpeg(cmd: list[str], on_progress: ProgressCallback | None = None) -> None:
    """Run ffmpeg, streaming ``-progress`` output to ``on_progress``."""
    if REGISTRY.cancelled.is_set():
        raise MasteringError("cancelled")
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    REGISTRY.add(proc)
    stderr_tail: deque[str] = deque(maxlen=40)

    def drain_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            if line.strip():
                stderr_tail.append(line.rstrip())

    drainer = threading.Thread(target=drain_stderr, daemon=True)
    drainer.start()
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            key, _, value = line.strip().partition("=")
            if key == "out_time_us" and on_progress is not None and value.lstrip("-").isdigit():
                on_progress(max(0, int(value)) / 1_000_000)
        returncode = proc.wait()
    finally:
        drainer.join(timeout=5)
        REGISTRY.discard(proc)

    if REGISTRY.cancelled.is_set():
        raise MasteringError("cancelled")
    if returncode != 0:
        detail = "\n".join(stderr_tail) or f"ffmpeg exited with code {returncode}"
        raise MasteringError(detail)


def _temp_path(output: Path) -> Path:
    return output.with_name(f".{output.stem}.cm-{uuid.uuid4().hex[:8]}.part{output.suffix}")


def master_file(
    input_path: Path,
    output_path: Path,
    cfg: MasterConfig,
    *,
    overwrite: bool = False,
    dry_run: bool = False,
    on_start: Callable[[MediaInfo, FilterPlan, list[str]], None] | None = None,
    on_progress: ProgressCallback | None = None,
    rng: random.Random | None = None,
) -> JobResult:
    """Probe, plan and master a single file. Never raises for per-file failures."""
    started = time.monotonic()
    input_path = input_path.expanduser().resolve()
    output_path = output_path.expanduser().resolve()
    info: MediaInfo | None = None
    plan: FilterPlan | None = None
    cmd: list[str] = []
    tmp: Path | None = None
    try:
        if input_path == output_path:
            raise MasteringError("output path must differ from the input path")
        if output_path.exists() and not overwrite and not dry_run:
            return JobResult(input_path, output_path, ok=False, skipped=True, message="output exists (use --overwrite)")
        muxer = muxer_for(output_path)
        require_encoder(cfg.codec)
        require_encoder("aac")

        info = probe(input_path)
        plan = build_plan(info, cfg, soxr=has_soxr(), rng=rng)
        tmp = None if dry_run else _temp_path(output_path)
        cmd = build_command(info, cfg, plan, tmp or output_path, muxer)
        if on_start is not None:
            on_start(info, plan, cmd)
        if dry_run:
            return JobResult(
                input_path, output_path, ok=True, message="dry run", command=tuple(cmd), plan=plan, info=info
            )

        assert tmp is not None
        output_path.parent.mkdir(parents=True, exist_ok=True)
        run_ffmpeg(cmd, on_progress)
        if muxer in _ISOBMFF_MUXERS:
            sanitize_sample_entries(tmp, cfg.vendor_id if muxer == "mov" else None)
        os.replace(tmp, output_path)
        tmp = None
        return JobResult(
            input_path,
            output_path,
            ok=True,
            message="done",
            elapsed=time.monotonic() - started,
            command=tuple(cmd),
            plan=plan,
            info=info,
        )
    except (MasteringError, ProbeError, ToolMissingError, ContainerPatchError, OSError) as exc:
        return JobResult(
            input_path,
            output_path,
            ok=False,
            message=str(exc),
            elapsed=time.monotonic() - started,
            command=tuple(cmd),
            plan=plan,
            info=info,
        )
    finally:
        if tmp is not None and tmp.exists():
            tmp.unlink(missing_ok=True)


def discover_videos(directory: Path, recursive: bool, exclude: Path | None = None) -> list[Path]:
    """List ``.mp4/.mov/.mkv`` files (case-insensitive), skipping hidden/partial files."""
    directory = directory.expanduser().resolve()
    if not directory.is_dir():
        raise MasteringError(f"not a directory: {directory}")
    pattern = "**/*" if recursive else "*"
    excluded = exclude.expanduser().resolve() if exclude else None
    found: list[Path] = []
    for path in directory.glob(pattern):
        if not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        if path.name.startswith(".") or ".cm-" in path.name:
            continue
        if excluded is not None and excluded != directory and excluded in path.parents:
            continue
        found.append(path)
    return sorted(found)


def output_for(source: Path, in_dir: Path, out_dir: Path, container: str, suffix: str) -> Path:
    """Mirror ``source``'s relative path under ``out_dir`` with the new container."""
    relative = source.resolve().relative_to(in_dir.expanduser().resolve())
    extension = source.suffix.lower() if container == "keep" else f".{container}"
    return out_dir.expanduser().resolve() / relative.parent / f"{relative.stem}{suffix}{extension}"


def run_batch(
    jobs: Iterable[tuple[Path, Path]],
    cfg: MasterConfig,
    *,
    workers: int = 1,
    overwrite: bool = False,
    dry_run: bool = False,
    on_start: Callable[[Path, MediaInfo, FilterPlan, list[str]], None] | None = None,
    on_progress: Callable[[Path, float], None] | None = None,
    on_finish: Callable[[JobResult], None] | None = None,
) -> list[JobResult]:
    """Master many files concurrently. ``KeyboardInterrupt`` stops all workers."""
    job_list = list(jobs)
    results: list[JobResult] = []

    def worker(src: Path, dst: Path) -> JobResult:
        return master_file(
            src,
            dst,
            cfg,
            overwrite=overwrite,
            dry_run=dry_run,
            on_start=(lambda info, plan, cmd: on_start(src, info, plan, cmd)) if on_start else None,
            on_progress=(lambda seconds: on_progress(src, seconds)) if on_progress else None,
        )

    pool = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="cine-master")
    try:
        futures = [pool.submit(worker, src, dst) for src, dst in job_list]
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            if on_finish is not None:
                on_finish(result)
    except KeyboardInterrupt:
        REGISTRY.terminate_all()
        pool.shutdown(wait=True, cancel_futures=True)
        raise
    pool.shutdown(wait=True)
    order = {src.expanduser().resolve(): i for i, (src, _) in enumerate(job_list)}
    return sorted(results, key=lambda r: order.get(r.input, 0))
