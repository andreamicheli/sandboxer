"""Per-run artifact isolation under ``artifacts/runs/<run_id>/``.

Historically every stage wrote stable names (``replay.json``,
``video-manifest.json``, ``delivery.mp4``, ...) straight into ``artifacts/``.
Two runs therefore shared one directory and could read each other's partial
outputs.  This module gives each run its own directory and canonical,
per-kind filenames so cross-run contamination becomes structurally
impossible.

A run directory is self-describing: ``write_run_manifest`` records the run id,
creation time and the sha256 of every input file consumed, and
``validate_run_dir`` re-checks those commitments later.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path

RUNS_DIRNAME = "runs"
MANIFEST_FILENAME = "run-manifest.json"
MANIFEST_SCHEMA = "sandboxer.run-manifest.v1"

# Canonical filename per artifact kind inside a run directory: <kind>.<ext>.
ARTIFACT_EXTENSIONS: Mapping[str, str] = {
    "evidence": ".json",
    "replay": ".json",
    "report": ".json",
    "report_html": ".html",
    "report_pdf": ".pdf",
    "video_manifest": ".json",
    "commentary_wav": ".wav",
    "video_only": ".mp4",
    "delivery": ".mp4",
    "broadcast": ".json",
}


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_dir(artifacts_root: str | os.PathLike[str], run_id: str) -> Path:
    """Return (creating if needed) ``<artifacts_root>/runs/<run_id>/``."""
    if not run_id or "/" in run_id or run_id in {".", ".."}:
        raise ValueError(f"RUN_ID_INVALID:{run_id!r}")
    directory = Path(artifacts_root) / RUNS_DIRNAME / run_id
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def run_artifact_path(directory: str | os.PathLike[str], kind: str) -> Path:
    """Canonical path for ``kind`` inside an existing run directory."""
    try:
        extension = ARTIFACT_EXTENSIONS[kind]
    except KeyError:
        raise ValueError(
            f"ARTIFACT_KIND_INVALID:{kind} (expected one of "
            f"{', '.join(sorted(ARTIFACT_EXTENSIONS))})"
        ) from None
    return Path(directory) / f"{kind}{extension}"


def write_run_manifest(
    directory: str | os.PathLike[str],
    run_id: str,
    inputs: Mapping[str, str | os.PathLike[str]] | Iterable[str | os.PathLike[str]],
) -> dict:
    """Record ``run_id`` plus the sha256 of every input file into the run dir.

    ``inputs`` is either a mapping of label to file path or a plain iterable of
    paths (labelled by their file name).  Returns the manifest that was written.
    """
    directory = Path(directory)
    entries: dict[str, dict[str, str]] = {}
    if isinstance(inputs, Mapping):
        items = ((str(label), path) for label, path in inputs.items())
    else:
        items = ((Path(path).name, path) for path in inputs)
    for label, path in items:
        path = Path(path)
        entries[label] = {"path": str(path), "sha256": _file_sha256(path)}
    manifest = {
        "schema_version": MANIFEST_SCHEMA,
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "inputs": entries,
    }
    (directory / MANIFEST_FILENAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def validate_run_dir(directory: str | os.PathLike[str]) -> list[str]:
    """Check a run directory's self-description; return human-readable problems.

    Detects: a missing manifest, a manifest ``run_id`` disagreeing with the
    directory name, and referenced input files whose recorded hash is missing
    or no longer matches the bytes on disk.  An empty problem list means clean.
    """
    directory = Path(directory)
    problems: list[str] = []
    manifest_path = directory / MANIFEST_FILENAME
    if not manifest_path.is_file():
        return [f"{MANIFEST_FILENAME}: missing"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return [f"{MANIFEST_FILENAME}: unreadable ({error})"]
    if not isinstance(manifest, dict):
        return [f"{MANIFEST_FILENAME}: not an object"]

    run_id = manifest.get("run_id")
    if run_id != directory.name:
        problems.append(f"run_id mismatch: manifest {run_id!r} vs directory {directory.name!r}")

    inputs = manifest.get("inputs")
    if not isinstance(inputs, dict):
        problems.append("inputs: missing or malformed")
        return problems
    for label, entry in inputs.items():
        entry = entry if isinstance(entry, dict) else {}
        path_text = entry.get("path")
        recorded = entry.get("sha256")
        if not isinstance(path_text, str):
            problems.append(f"input {label}: missing path")
            continue
        path = Path(path_text)
        if not path.is_file():
            continue  # vanished inputs are the caller's lifecycle concern
        if not isinstance(recorded, str) or not recorded:
            problems.append(f"input {label}: existing file has no recorded hash")
            continue
        if _file_sha256(path) != recorded:
            problems.append(f"input {label}: sha256 mismatch for {path}")
    return problems


@dataclass(frozen=True)
class RunLayout:
    """Handle bundling a created run directory with its run id."""

    directory: Path
    run_id: str

    def artifact(self, kind: str) -> Path:
        return run_artifact_path(self.directory, kind)

    @property
    def manifest_path(self) -> Path:
        return self.directory / MANIFEST_FILENAME

    def write_manifest(self, inputs: Mapping[str, str | os.PathLike[str]] | Iterable[str | os.PathLike[str]]) -> dict:
        return write_run_manifest(self.directory, self.run_id, inputs)

    def validate(self) -> list[str]:
        return validate_run_dir(self.directory)


def get_run_layout(artifacts_root: str | os.PathLike[str], run_id: str) -> RunLayout:
    """Create (if needed) and return the layout handle for ``run_id``.

    Entry point for other modules adopting per-run isolation; callers keep
    working unchanged until they switch over.
    """
    return RunLayout(directory=run_dir(artifacts_root, run_id), run_id=run_id)
