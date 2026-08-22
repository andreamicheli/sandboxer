"""Tests for per-run artifact isolation (sandboxer_v0.run_layout)."""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path

import pytest

from sandboxer_v0.run_layout import (
    MANIFEST_FILENAME,
    RUNS_DIRNAME,
    get_run_layout,
    run_artifact_path,
    run_dir,
    validate_run_dir,
    write_run_manifest,
)


def _sha256(path):
    return sha256(path.read_bytes()).hexdigest()


def _make_input(tmp_path, name, payload="payload\n"):
    path = tmp_path / name
    path.write_text(payload, encoding="utf-8")
    return path


def test_canonical_filenames_cover_every_kind():
    expected = {
        "evidence": "evidence.json",
        "replay": "replay.json",
        "report": "report.json",
        "report_html": "report_html.html",
        "report_pdf": "report_pdf.pdf",
        "video_manifest": "video_manifest.json",
        "commentary_wav": "commentary_wav.wav",
        "video_only": "video_only.mp4",
        "delivery": "delivery.mp4",
        "broadcast": "broadcast.json",
    }
    run = Path("/artifacts/runs/r1")
    for kind, filename in expected.items():
        assert run_artifact_path(run, kind) == run / filename


def test_unknown_artifact_kind_is_rejected():
    with pytest.raises(ValueError, match="ARTIFACT_KIND_INVALID"):
        run_artifact_path(Path("/artifacts/runs/r1"), "staging")


def test_run_dir_creates_nested_directory_idempotently(tmp_path):
    first = run_dir(tmp_path, "match-42")
    second = run_dir(tmp_path, "match-42")
    assert first == second == tmp_path / RUNS_DIRNAME / "match-42"
    assert first.is_dir()
    with pytest.raises(ValueError, match="RUN_ID_INVALID"):
        run_dir(tmp_path, "../escape")


def test_manifest_roundtrip_records_hashes_and_utc_created_at(tmp_path):
    telemetry = _make_input(tmp_path, "match.telemetry.jsonl", '{"kind": "match_started"}\n')
    result = _make_input(tmp_path, "match.result.json", '{"winner": "a"}')
    directory = run_dir(tmp_path, "match-7")

    write_run_manifest(directory, "match-7", {"telemetry": telemetry, "result": result})

    manifest = json.loads((directory / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert manifest["run_id"] == "match-7"
    created_at = datetime.fromisoformat(manifest["created_at"].replace("Z", "+00:00"))
    assert created_at.tzinfo is not None and manifest["created_at"].endswith("Z")
    assert manifest["inputs"]["telemetry"]["path"] == str(telemetry)
    assert manifest["inputs"]["telemetry"]["sha256"] == _sha256(telemetry)
    assert manifest["inputs"]["result"]["sha256"] == _sha256(result)


def test_get_run_layout_wires_helpers_together(tmp_path):
    layout = get_run_layout(tmp_path, "match-9")
    assert layout.directory == tmp_path / RUNS_DIRNAME / "match-9"
    assert layout.artifact("delivery") == layout.directory / "delivery.mp4"
    input_path = _make_input(tmp_path, "in.jsonl")
    layout.write_manifest({"input": input_path})
    assert layout.manifest_path.is_file()
    assert validate_run_dir(layout.directory) == []


def test_validate_clean_run_dir_has_no_problems(tmp_path):
    directory = run_dir(tmp_path, "match-1")
    telemetry = _make_input(tmp_path, "t.jsonl")
    write_run_manifest(directory, "match-1", [telemetry])
    assert validate_run_dir(directory) == []


def test_validate_detects_missing_manifest(tmp_path):
    directory = run_dir(tmp_path, "match-2")
    problems = validate_run_dir(directory)
    assert len(problems) == 1 and "missing" in problems[0]


def test_validate_detects_run_id_mismatch_with_directory_name(tmp_path):
    directory = run_dir(tmp_path, "match-3")
    write_run_manifest(directory, "match-other", [])
    problems = validate_run_dir(directory)
    assert any("run_id mismatch" in problem for problem in problems)


def test_validate_detects_hash_mismatch_for_tampered_input(tmp_path):
    directory = run_dir(tmp_path, "match-4")
    telemetry = _make_input(tmp_path, "t.jsonl", "original bytes")
    write_run_manifest(directory, "match-4", {"telemetry": telemetry})

    telemetry.write_text("tampered bytes", encoding="utf-8")

    problems = validate_run_dir(directory)
    assert any("sha256 mismatch" in problem for problem in problems)


def test_validate_detects_missing_hash_for_existing_input(tmp_path):
    directory = run_dir(tmp_path, "match-5")
    telemetry = _make_input(tmp_path, "t.jsonl")
    write_run_manifest(directory, "match-5", {"telemetry": telemetry})
    manifest_path = directory / MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["inputs"]["telemetry"]["sha256"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    problems = validate_run_dir(directory)

    assert any("no recorded hash" in problem for problem in problems)


# --- build_real_artifacts.py CLI integration -------------------------------

# Small, valid drafts so main() exercises the manifest path deterministically
# without invoking any headless agent backend.
_SHORT_INTRO = [
    {"voice_role": "play_by_play", "line_type": "editorial", "scene": "cold_open",
     "offset_seconds": 1.0, "text": "Welcome to Sandboxer."},
    {"voice_role": "analyst", "line_type": "interpreted", "scene": "model_cards_and_rules",
     "offset_seconds": 1.0, "model": "Laguna S 2.1", "text": "It seems very sharp today."},
]
_SHORT_COMMENTARY = [
    {"voice_role": "play_by_play", "line_type": "observed", "event_ids": ["e01"],
     "text": "We are live."},
    {"voice_role": "analyst", "line_type": "interpreted", "event_ids": ["e04"],
     "text": "It seems strategy decided the match."},
]


@pytest.fixture(name="match_inputs")
def _match_inputs(tmp_path, monkeypatch):
    from scripts import build_real_artifacts as bra

    monkeypatch.setattr(bra, "draft_intro_commentary", lambda *args, **kwargs: [dict(line) for line in _SHORT_INTRO])
    monkeypatch.setattr(bra, "draft_commentary", lambda *args, **kwargs: [dict(line) for line in _SHORT_COMMENTARY])
    telemetry = tmp_path / "m.telemetry.jsonl"
    telemetry.write_text(
        "\n".join(
            json.dumps(event)
            for event in (
                {"monotonic_ns": 1_000, "kind": "match_started"},
                {
                    "monotonic_ns": 20_000_000_000,
                    "kind": "tool_decision",
                    "tool": "http_request",
                    "model": "poolside/laguna-s-2.1-free",
                    "phase": "red",
                },
                {"monotonic_ns": 40_000_000_000, "kind": "match_finished"},
            )
        )
        + "\n",
        encoding="utf-8",
    )
    result = tmp_path / "m.result.json"
    result.write_text(
        json.dumps(
            {
                "winner": "poolside/laguna-s-2.1-free",
                "outcome": "win",
                "reason_code": "FLAG_CAPTURED",
                "captures": [],
                "defenses": {},
            }
        ),
        encoding="utf-8",
    )
    return bra, telemetry, result


def test_build_real_artifacts_without_flags_keeps_legacy_flat_layout(tmp_path, match_inputs):
    bra, telemetry, result = match_inputs
    out_dir = tmp_path / "artifacts"

    assert bra.main([
        "--telemetry", str(telemetry),
        "--result", str(result),
        "--out-dir", str(out_dir),
    ]) == 0

    assert sorted(path.name for path in out_dir.glob("*")) == [
        "remotion-props.json", "replay.json", "report.json", "video-manifest.json",
    ]
    assert not (out_dir / RUNS_DIRNAME).exists()


def test_build_real_artifacts_run_id_places_outputs_in_isolated_run_dir(tmp_path, match_inputs):
    bra, telemetry, result = match_inputs
    root = tmp_path / "root"

    assert bra.main([
        "--telemetry", str(telemetry),
        "--result", str(result),
        "--artifacts-root", str(root),
        "--run-id", "match-77",
    ]) == 0

    run = root / RUNS_DIRNAME / "match-77"
    assert sorted(path.name for path in run.glob("*")) == [
        "remotion-props.json", "replay.json", "report.json", MANIFEST_FILENAME, "video_manifest.json",
    ]
    assert json.loads((run / "video_manifest.json").read_text(encoding="utf-8"))["fps"] == 30
    assert validate_run_dir(run) == []
    manifest = json.loads((run / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert manifest["run_id"] == "match-77"
    assert manifest["inputs"]["telemetry"]["path"] == str(telemetry)
    assert manifest["inputs"]["telemetry"]["sha256"] == _sha256(telemetry)
