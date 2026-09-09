"""Audio silence QA gate for rendered SandBoxer broadcasts.

Usage:

    python scripts/qa_audio_gate.py --video ../artifacts/broadcast.mp4
    python scripts/qa_audio_gate.py --video out.mp4 --max-silent-ratio 0.05

Behaviour: probe the container duration via ffprobe, then sample mean_volume
with ffmpeg volumedetect over windows of ``--sample-len`` seconds starting
every ``--sample-step`` seconds.  A window counts as silent when its
mean_volume is at or below -60 dB.  The gate passes when the silent fraction
is at most ``--max-silent-ratio`` (default 0.10), printing
``AUDIO_GATE_PASS (silent X/Y samples)`` and exiting 0; otherwise it prints
``AUDIO_GATE_FAIL`` with the silent window timestamps and exits 3.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Sequence

SILENCE_FLOOR_DB = -60.0
DEFAULT_MAX_SILENT_RATIO = 0.10
DEFAULT_SAMPLE_STEP = 60
DEFAULT_SAMPLE_LEN = 4


class AudioGateError(RuntimeError):
    """Raised when the audio timeline cannot be probed or sampled."""


def _probe_duration(video: Path, run: Callable[..., subprocess.CompletedProcess]) -> float:
    """Container duration in seconds; fails closed when unmeasurable."""
    try:
        completed = run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "json", str(video)],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise AudioGateError(f"ffprobe failed for {video}: {error}") from error
    if completed.returncode != 0:
        raise AudioGateError(
            f"ffprobe failed for {video}: {completed.stderr.strip()[:200]}"
        )
    try:
        duration = float(json.loads(completed.stdout)["format"]["duration"])
    except (KeyError, TypeError, ValueError) as error:
        raise AudioGateError(f"duration unavailable for {video}") from error
    if duration <= 0:
        raise AudioGateError(f"non-positive duration for {video}: {duration}")
    return duration


def _sample_mean_volume(
    video: Path,
    start: float,
    length: float,
    run: Callable[..., subprocess.CompletedProcess],
) -> float | None:
    """Mean volume in dB over [start, start+length); None without readable audio."""
    try:
        completed = run(
            ["ffmpeg", "-hide_banner", "-nostats",
             "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(video),
             "-map", "0:a:0", "-af", "volumedetect", "-f", "null", "-"],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise AudioGateError(f"ffmpeg volumedetect failed at {start:.0f}s: {error}") from error
    match = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", completed.stderr)
    return float(match.group(1)) if match else None


def _sample_windows(duration: float, step: int, length: int):
    """Deterministic (start, clamped_length) windows covering the timeline."""
    for index in range(max(1, math.ceil(duration / step))):
        start = index * step
        yield start, min(float(length), duration - start)


def check_video(
    video: str | Path,
    max_silent_ratio: float = DEFAULT_MAX_SILENT_RATIO,
    sample_step: int = DEFAULT_SAMPLE_STEP,
    sample_len: int = DEFAULT_SAMPLE_LEN,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> tuple[bool, str]:
    """Gate core: (passed, verdict line). Verdict starts with AUDIO_GATE_*."""
    path = Path(video)
    try:
        duration = _probe_duration(path, run)
    except AudioGateError as error:
        return False, f"AUDIO_GATE_FAIL: {error}"
    starts: list[float] = []
    volumes: list[float] = []
    for start, length in _sample_windows(duration, sample_step, sample_len):
        try:
            volume = _sample_mean_volume(path, start, length, run)
        except AudioGateError as error:
            return False, f"AUDIO_GATE_FAIL: {error}"
        if volume is None:
            return False, (
                f"AUDIO_GATE_FAIL: no readable audio stream in {path} "
                f"(mean_volume missing near {start:.0f}s)"
            )
        starts.append(start)
        volumes.append(volume)
    silent = [start for start, volume in zip(starts, volumes) if volume <= SILENCE_FLOOR_DB]
    summary = f"(silent {len(silent)}/{len(starts)} samples)"
    if len(silent) / len(starts) <= max_silent_ratio:
        return True, f"AUDIO_GATE_PASS {summary}"
    stamps = ", ".join(f"{start:.0f}s" for start in silent)
    return False, (
        f"AUDIO_GATE_FAIL {summary} exceeds max-silent-ratio "
        f"{max_silent_ratio}; silent sample timestamps: {stamps}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audio silence QA gate")
    parser.add_argument("--video", required=True, type=Path)
    parser.add_argument("--max-silent-ratio", type=float, default=DEFAULT_MAX_SILENT_RATIO,
                        help=f"silent-fraction threshold (default: {DEFAULT_MAX_SILENT_RATIO})")
    parser.add_argument("--sample-step", type=int, default=DEFAULT_SAMPLE_STEP,
                        help=f"seconds between sample windows (default: {DEFAULT_SAMPLE_STEP})")
    parser.add_argument("--sample-len", type=int, default=DEFAULT_SAMPLE_LEN,
                        help=f"seconds per sample window (default: {DEFAULT_SAMPLE_LEN})")
    args = parser.parse_args(argv)
    passed, verdict = check_video(
        args.video,
        max_silent_ratio=args.max_silent_ratio,
        sample_step=args.sample_step,
        sample_len=args.sample_len,
    )
    print(verdict)
    return 0 if passed else 3


if __name__ == "__main__":
    sys.exit(main())
