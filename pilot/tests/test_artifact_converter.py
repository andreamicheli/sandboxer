"""Tests for the canonical telemetry-to-artifact conversion phase."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from hashlib import sha256
from pathlib import Path

import pytest

from sandboxer_v0.artifact_converter import (
    EVIDENCE_SCHEMA,
    REPLAY_SCHEMA,
    CanonicalEvidence,
    CanonicalReplay,
    convert,
    validate_artifacts,
    write_artifacts,
)

SEED = "match-2026-08-22-a"


def _telemetry() -> list[dict]:
    """Minimal fixture shaped exactly like run_command_code_match telemetry."""
    return [
        {
            "monotonic_ns": 1_000_000_000, "kind": "match_started",
            "models": ["poolside/laguna-s-2.1-free", "meta/muse-spark-1.2-contributor"],
            "publication_enabled": False, "provider": "command_code",
            "seed": SEED, "blue_brief_manifest": {"family": "probe-first"},
            "tool_ceilings": {"blue": 8, "red": 10},
        },
        # High-volume provider noise that the conversion must skip.
        {
            "monotonic_ns": 1_100_000_000, "kind": "provider_frame",
            "model": "poolside/laguna-s-2.1-free", "phase": "blue",
            "frame_type": "event", "event_type": "assistant_message",
        },
        {
            "monotonic_ns": 2_000_000_000, "kind": "tool_decision",
            "competitor": "poolside/laguna-s-2.1-free", "phase": "blue",
            "tool": "deploy_service", "allowed": True, "reason_code": None,
        },
        {
            "monotonic_ns": 3_000_000_000, "kind": "deployment_promoted",
            "model": "poolside/laguna-s-2.1-free", "graph_hash": "a" * 64,
            "protected_policy": "header", "recovery_posture": "header",
        },
        {"monotonic_ns": 4_000_000_000, "kind": "blue_finished", "usage": []},
        {"monotonic_ns": 5_000_000_000, "kind": "interview_finished",
         "tool_access": False, "usage": []},
        {
            "monotonic_ns": 6_000_000_000, "kind": "tool_decision",
            "competitor": "meta/muse-spark-1.2-contributor", "phase": "red",
            "tool": "describe_target_service", "allowed": True, "reason_code": None,
        },
        {
            "monotonic_ns": 7_000_000_000, "kind": "tool_decision",
            "competitor": "meta/muse-spark-1.2-contributor", "phase": "red",
            "tool": "http_request", "allowed": True, "reason_code": None,
        },
        {
            "monotonic_ns": 8_500_000_000, "kind": "tool_decision",
            "competitor": "meta/muse-spark-1.2-contributor", "phase": "red",
            "tool": "submit_flag", "allowed": True, "reason_code": None,
        },
        # Decisions from undeclared models carry no pane and are dropped.
        {
            "monotonic_ns": 8_700_000_000, "kind": "tool_decision",
            "competitor": "unknown/model-z", "phase": "red",
            "tool": "http_request", "allowed": False,
            "reason_code": "PHASE_TOOL_NOT_ALLOWED",
        },
        {
            "monotonic_ns": 9_000_000_000, "kind": "match_finished",
            "winner": "poolside/laguna-s-2.1-free", "captures": [True, False],
            "outcome": "VALID_CAPTURE", "reason_code": "SOLE_CAPTURE",
            "publication_enabled": False, "is_calibration": True,
        },
        {"monotonic_ns": 10_000_000_000, "kind": "teardown",
         "states": ["destroyed", "destroyed"]},
    ]


def _result() -> dict:
    """Minimal fixture shaped exactly like run_command_code_match result.json."""
    return {
        "result": "passed", "outcome": "VALID_CAPTURE", "reason_code": "SOLE_CAPTURE",
        "publication_enabled": False, "is_calibration": True, "provider": "command_code",
        "models": ["poolside/laguna-s-2.1-free", "meta/muse-spark-1.2-contributor"],
        "winner": "poolside/laguna-s-2.1-free", "captures": [True, False],
        "defenses": {
            "poolside/laguna-s-2.1-free": {
                "graph_hash": "a" * 64, "protected_policy": "header", "recovery_posture": "header",
            },
            "meta/muse-spark-1.2-contributor": {
                "graph_hash": "b" * 64, "protected_policy": "deny", "recovery_posture": "public",
            },
        },
        "seed": SEED,
        "tool_counts": {}, "tool_names": {},
        "calibration_only": True,
    }


def _converted() -> tuple[CanonicalEvidence, CanonicalReplay]:
    return convert(_telemetry(), _result())


def test_convert_maps_runtime_schema_to_canonical_evidence():
    evidence, _ = _converted()

    assert evidence.schema_version == EVIDENCE_SCHEMA
    assert evidence.match_id == SEED
    assert evidence.run_id == SEED
    assert evidence.competitors == ["Laguna S 2.1", "Muse Spark 1.2"]
    assert evidence.phases == ["blue", "red"]
    assert evidence.outcome == {
        "winner": "Laguna S 2.1",
        "reason_code": "SOLE_CAPTURE",
        "outcome": "VALID_CAPTURE",
        "captures": [True, False],
    }
    assert evidence.defenses == {
        "Laguna S 2.1": {"protected_policy": "header", "recovery_posture": "header"},
        "Muse Spark 1.2": {"protected_policy": "deny", "recovery_posture": "public"},
    }

    assert len(evidence.events) == 11
    assert [event["event_id"] for event in evidence.events] == [f"e{n:02d}" for n in range(1, 12)]
    assert [event["event_type"] for event in evidence.events] == [
        "MATCH_STARTED", "MATCH_STARTED", "TOOL_CALL", "MODEL_RESPONSE",
        "PHASE_TRANSITION", "PHASE_TRANSITION", "TOOL_CALL", "TOOL_CALL",
        "TOOL_CALL", "MATCH_FINISHED", "MATCH_FINISHED",
    ]

    # Tool calls are redacted structurally: no arguments or raw responses exist.
    assert len(evidence.tool_calls) == 4
    for tool_call in evidence.tool_calls:
        assert set(tool_call) == {
            "competitor", "pane", "phase", "tool", "allowed", "reason_code", "text",
            "at_monotonic_ns",
        }
    assert [(call["tool"], call["phase"], call["allowed"]) for call in evidence.tool_calls] == [
        ("deploy_service", "blue", True),
        ("describe_target_service", "red", True),
        ("http_request", "red", True),
        ("submit_flag", "red", True),
    ]
    assert evidence.tool_calls[2]["text"] == "probe: HTTP request on target"

    assert [(attack["attacker"], attack["kind"]) for attack in evidence.attacks] == [
        ("Muse Spark 1.2", "target_recon"),
        ("Muse Spark 1.2", "http_probe"),
        ("Muse Spark 1.2", "flag_submission"),
    ]
    assert [attack["at_monotonic_ns"] for attack in evidence.attacks] == [
        6_000_000_000, 7_000_000_000, 8_500_000_000,
    ]


def test_convert_derives_replay_frames_from_events_with_deterministic_timestamps():
    evidence, replay = _converted()

    assert replay.schema_version == REPLAY_SCHEMA
    assert replay.source_bundle_hash == SEED.ljust(64, "0")
    assert replay.panes == [{"identity": "Laguna S 2.1"}, {"identity": "Muse Spark 1.2"}]
    assert replay.layout == {"split": {"left": 0.5, "right": 0.5, "permanent": True}}

    assert len(replay.frames) == len(evidence.events) == 11
    for position, (frame, event) in enumerate(zip(replay.frames, evidence.events), start=1):
        assert frame["sequence"] == position
        assert frame["event_id"] == event["event_id"] == f"e{position:02d}"
        assert frame["at_monotonic_ns"] == event["at_monotonic_ns"]
        assert frame["event_type"] == event["event_type"]
        assert frame["phase"] == event["phase"]
        assert frame["pane"] == event["pane"]
        assert frame["match_number"] == 1
    timestamps = [frame["at_monotonic_ns"] for frame in replay.frames]
    assert timestamps == sorted(timestamps)
    assert timestamps[0] == 1_000_000_000 and timestamps[-1] == 10_000_000_000


def test_convert_serialization_key_order_is_stable_for_downstream_manifests():
    evidence, replay = _converted()
    evidence_dict = evidence.to_dict()
    replay_dict = replay.to_dict()

    assert list(evidence_dict) == [
        "schema_version", "run_id", "match_id", "competitors", "phases", "events",
        "outcome", "tool_calls", "defenses", "attacks",
    ]
    assert list(replay_dict) == [
        "schema_version", "source_bundle_hash", "panes", "layout", "frames",
    ]
    assert list(replay_dict["frames"][0]) == [
        "sequence", "at_monotonic_ns", "event_id", "event_type", "phase", "pane",
        "text", "match_number",
    ]


def test_convert_is_deterministic():
    first_evidence, first_replay = _converted()
    second_evidence, second_replay = convert(_telemetry(), _result())

    assert first_evidence.to_dict() == second_evidence.to_dict()
    assert first_replay.to_dict() == second_replay.to_dict()


def test_convert_fallback_defines_frame_lists_before_use():
    _, empty_replay = convert([], _result())
    _, noise_replay = convert(
        [{"monotonic_ns": 5, "kind": "provider_frame", "model": "poolside/laguna-s-2.1-free"}],
        _result(),
    )

    # The fallback derives its deterministic timestamps from the first
    # telemetry record (0 when no records exist at all).
    assert [frame["at_monotonic_ns"] for frame in empty_replay.frames] == [0, 1]
    assert [frame["at_monotonic_ns"] for frame in noise_replay.frames] == [5, 6]
    for replay in (empty_replay, noise_replay):
        assert [frame["sequence"] for frame in replay.frames] == [1, 2]
        assert [frame["event_type"] for frame in replay.frames] == ["MATCH_STARTED", "MATCH_FINISHED"]
        assert [frame["text"] for frame in replay.frames] == ["match started", "match finished"]


def test_canonical_artifacts_are_frozen():
    evidence, replay = _converted()

    with pytest.raises(FrozenInstanceError):
        evidence.run_id = "tampered"
    with pytest.raises(FrozenInstanceError):
        replay.schema_version = "sandboxer.replay.v2"


def test_write_artifacts_hash_roundtrip(tmp_path):
    evidence, replay = _converted()

    digests = write_artifacts(evidence, replay, tmp_path)

    assert set(digests) == {"evidence.json", "replay.json"}
    for filename, digest in digests.items():
        assert sha256((tmp_path / filename).read_bytes()).hexdigest() == digest
    ledger = (tmp_path / "artifacts.sha256").read_text(encoding="utf-8")
    assert ledger == (
        f"{digests['evidence.json']}  evidence.json\n"
        f"{digests['replay.json']}  replay.json\n"
    )
    assert validate_artifacts(tmp_path) == []


@pytest.fixture(name="artifact_dir")
def _artifact_dir(tmp_path):
    evidence, replay = _converted()
    write_artifacts(evidence, replay, tmp_path)
    return tmp_path


def test_validate_artifacts_detects_tampered_replay(artifact_dir):
    replay_path = artifact_dir / "replay.json"
    replay_path.write_bytes(replay_path.read_bytes().replace(b"Laguna S 2.1", b"Laguna S 2.0"))

    problems = validate_artifacts(artifact_dir)

    assert any("replay.json: sha256 mismatch" in problem for problem in problems)
    assert not any(problem.startswith("evidence.json: sha256") for problem in problems)


def test_validate_artifacts_detects_missing_required_fields_and_schema_drift(artifact_dir):
    evidence_path = artifact_dir / "evidence.json"
    document = json.loads(evidence_path.read_text(encoding="utf-8"))
    del document["run_id"]
    document["schema_version"] = "sandboxer.evidence.v0"
    evidence_path.write_text(json.dumps(document, indent=2), encoding="utf-8")

    problems = validate_artifacts(artifact_dir)

    assert any("missing required field 'run_id'" in problem for problem in problems)
    assert any("schema_version" in problem for problem in problems)
    assert any("evidence.json: sha256 mismatch" in problem for problem in problems)


def test_validate_artifacts_reports_missing_files(tmp_path):
    problems = validate_artifacts(tmp_path)

    assert problems == ["evidence.json: missing", "replay.json: missing", "artifacts.sha256: missing"]


def test_validate_artifacts_flags_unreadable_json(artifact_dir):
    (artifact_dir / "replay.json").write_text("{not json", encoding="utf-8")

    problems = validate_artifacts(artifact_dir)

    assert any("replay.json: unreadable" in problem for problem in problems)


def test_build_real_artifacts_delegates_replay_conversion_to_the_module(tmp_path, monkeypatch):
    from scripts import build_real_artifacts as bra

    monkeypatch.setattr(bra, "draft_intro_commentary", lambda *args, **kwargs: [
        {"voice_role": "play_by_play", "line_type": "editorial", "scene": "cold_open",
         "offset_seconds": 1.0, "text": "Welcome to Sandboxer."},
    ])
    monkeypatch.setattr(bra, "draft_commentary", lambda *args, **kwargs: [
        {"voice_role": "play_by_play", "line_type": "observed", "event_ids": ["e01"],
         "text": "We are live."},
        {"voice_role": "analyst", "line_type": "interpreted", "event_ids": ["e11"],
         "text": "It seems strategy decided the match."},
    ])
    telemetry_path = tmp_path / "m.telemetry.jsonl"
    telemetry_path.write_text(
        "".join(json.dumps(event) + "\n" for event in _telemetry()), encoding="utf-8",
    )
    result_path = tmp_path / "m.result.json"
    result_path.write_text(json.dumps(_result()), encoding="utf-8")
    out_dir = tmp_path / "artifacts"

    assert bra.main([
        "--telemetry", str(telemetry_path),
        "--result", str(result_path),
        "--out-dir", str(out_dir),
    ]) == 0

    _, expected_replay = convert(_telemetry(), _result())
    written_replay = json.loads((out_dir / "replay.json").read_text(encoding="utf-8"))
    assert written_replay == expected_replay.to_dict()
