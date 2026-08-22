"""Resource preflight for the Remotion video render.

A render can fail purely on host resources: Remotion keeps roughly one
Chromium renderer alive per ``--concurrency`` lane, and it spills frame data
into ``TMPDIR`` — so a machine with little RAM, or a RAM-backed/tmpfs temp
dir with less than a few GiB free, dies mid-render no matter how correct the
manifest was (the documented OOM failure mode).  :func:`preflight_render`
measures both up front and returns a :class:`RenderPreflightPlan`:

- ``concurrency`` from total RAM: 1 lane under 6 GiB, 2 under 12 GiB,
  otherwise 4 (fail closed to 1 when RAM cannot be detected),
- a coarse ``quality`` recommendation tied to that tier,
- ``chosen_tmpdir``: the requested dir unless it sits on tmpfs or has under
  5 GiB free, in which case a persistent-disk directory under
  ``pilot/logs/render-tmp`` is created and used instead (with a warning),
- every deviation recorded in ``warnings`` — never silent.

:func:`sandboxer_v0.video.remotion_render_command` turns a plan into the
``npx remotion render`` argv so ``--concurrency`` is always passed.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

GiB = 1024**3

# RAM -> max concurrent Chromium render lanes.  Each lane holds a browser
# tab plus frame buffers, so low-RAM hosts must render serially.
_CONCURRENCY_TIERS: tuple[tuple[int, int], ...] = ((6 * GiB, 1), (12 * GiB, 2))
_MAX_CONCURRENCY = 4

#: Below this much free space a temp dir cannot safely hold render spill.
_MIN_TMPDIR_FREE_BYTES = 5 * GiB

# Persistent-disk fallback for a tmpfs/cramped TMPDIR (created on demand).
PERSISTENT_TMP_ROOT = Path(__file__).resolve().parents[2] / "pilot" / "logs" / "render-tmp"

#: Coarse render-quality recommendation for each concurrency tier.
QUALITY_BY_CONCURRENCY = {1: "low", 2: "medium", 4: "high"}


@dataclass(frozen=True)
class RenderPreflightPlan:
    """What the host can sustain for one Remotion render invocation."""

    ram_total_bytes: int | None
    concurrency: int
    quality: str
    requested_tmpdir: Path
    chosen_tmpdir: Path
    tmpdir_is_tmpfs: bool
    tmpdir_free_bytes: int | None
    warnings: tuple[str, ...]


def _meminfo_ram_bytes(text: str) -> int | None:
    """Parse ``MemTotal`` (kB) out of /proc/meminfo content into bytes."""
    for line in text.splitlines():
        if line.startswith("MemTotal:"):
            fields = line.split()
            if len(fields) >= 2 and fields[1].isdigit():
                return int(fields[1]) * 1024
            return None
    return None


def _ram_total() -> int | None:
    """Total RAM in bytes from /proc/meminfo, or None when undetectable."""
    try:
        text = Path("/proc/meminfo").read_text(encoding="utf-8")
    except OSError:
        return None
    return _meminfo_ram_bytes(text)


def _mount_table(text: str) -> tuple[tuple[str, str], ...]:
    """Parse a /proc/self/mounts-style table into (mount_point, fstype) pairs."""
    entries = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 3:
            entries.append((fields[1].replace("\\040", " "), fields[2]))
    return tuple(entries)


def _filesystem_type(path: Path, mounts_text: str | None = None) -> str | None:
    """Filesystem type backing ``path`` (longest matching mount point)."""
    if mounts_text is None:
        try:
            mounts_text = Path("/proc/self/mounts").read_text(encoding="utf-8")
        except OSError:
            return None
    candidate = os.fspath(path)
    best_type: str | None = None
    best_length = -1
    for mount_point, fstype in _mount_table(mounts_text):
        covers = mount_point == "/" or (
            candidate == mount_point or candidate.startswith(mount_point.rstrip("/") + "/")
        )
        if covers and len(mount_point) > best_length:
            best_type, best_length = fstype, len(mount_point)
    return best_type


def _free_bytes(path: Path) -> int | None:
    """Free bytes on the filesystem holding ``path``, or None when unknown."""
    try:
        return shutil.disk_usage(os.fspath(path)).free
    except OSError:
        return None


def preflight_render(
    ram_total_bytes: int | None = None,
    tmpdir: str | os.PathLike[str] | None = None,
    *,
    persistent_root: str | os.PathLike[str] | None = None,
) -> RenderPreflightPlan:
    """Measure RAM and the effective temp dir and plan a safe render.

    ``ram_total_bytes=None`` auto-detects via /proc/meminfo; ``tmpdir=None``
    uses ``$TMPDIR`` (falling back to ``/tmp``).  When the effective temp dir
    is tmpfs or has under 5 GiB free, a directory under
    ``pilot/logs/render-tmp`` is created on persistent disk and used instead;
    every substitution lands in ``warnings``.
    """
    warnings: list[str] = []

    ram = ram_total_bytes if ram_total_bytes is not None else _ram_total()
    if ram is None:
        concurrency = 1  # fail closed: assume the most constrained host
        warnings.append("RAM_UNKNOWN: /proc/meminfo unavailable; rendering with one lane")
    else:
        concurrency = _MAX_CONCURRENCY
        for ceiling, lanes in _CONCURRENCY_TIERS:
            if ram < ceiling:
                concurrency = lanes
                break
    quality = QUALITY_BY_CONCURRENCY[concurrency]

    requested = Path(tmpdir) if tmpdir is not None else Path(os.environ.get("TMPDIR") or "/tmp")
    fs_type = _filesystem_type(requested)
    free = _free_bytes(requested)
    is_tmpfs = fs_type == "tmpfs"
    cramped = free is not None and free < _MIN_TMPDIR_FREE_BYTES
    if is_tmpfs or cramped:
        root = Path(persistent_root) if persistent_root is not None else PERSISTENT_TMP_ROOT
        root.mkdir(parents=True, exist_ok=True)
        if is_tmpfs:
            warnings.append(
                f"TMPDIR_TMPFS: {requested} is RAM-backed tmpfs; "
                f"switching to persistent disk at {root}"
            )
        if cramped:
            warnings.append(
                f"TMPDIR_LOW_SPACE: {requested} has {free} bytes free "
                f"(< {_MIN_TMPDIR_FREE_BYTES}); switching to {root}"
            )
        chosen = root
    else:
        chosen = requested

    return RenderPreflightPlan(
        ram_total_bytes=ram,
        concurrency=concurrency,
        quality=quality,
        requested_tmpdir=requested,
        chosen_tmpdir=chosen,
        tmpdir_is_tmpfs=is_tmpfs,
        tmpdir_free_bytes=free,
        warnings=tuple(warnings),
    )
