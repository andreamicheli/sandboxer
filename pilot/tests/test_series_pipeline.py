"""Best-of-3 series pipeline: run_series driver + build_real_artifacts --series.

Covers the episode contract that one broadcast contains THREE matches:
synthetic 3-match telemetry is merged into a single video manifest with
cumulative frame offsets and intermission cards, the series winner logic is
decided deterministically (2-0 / 2-1 / 1-1-1 token tie-break), and the
single-match pipeline stays byte-identical to the legacy composition.

No test here launches QEMU: the match runner is injected or bypassed.
"""

from __future__ import annotations

import argparse
import json

import pytest

from sandboxer_v0.artifact_converter import IDENTITY_A, IDENTITY_B, convert
from sandboxer_v0.arena_visual import FakeArenaVisualDrafter, draft_arena_plan
from sandboxer_v0.commentary import build_commentary, draft_intro_commentary
from sandboxer_v0.series_result import (
    BASIS_FIRST_TO_TWO,
    BASIS_TOKEN_TIEBREAK,
    BASIS_UNRESOLVED,
    SERIES_SCHEMA,
    build_series_summary,
    decide_series_winner,
    identity_total_tokens,
    series_report_outcome,
)
from scripts.build_real_artifacts import (
    DEFAULT_BENCHMARK_DATA,
    ArtifactInputError,
    build_broadcast_artifacts,
    build_report,
    merge_series_replays,
    load_benchmark_snapshot,
)
from scripts.run_command_code_match import MatchCalibrationError, run_one_match
from scripts.run_series import SERIES_MATCHES, _per_match_args, execute_series
from sandboxer_v0.video import build_video_manifest

MODEL_A = "poolside/laguna-s-2.1-free"
MODEL_B = "meta/muse-spark-1.2-contributor"
MODELS = (MODEL_A, MODEL_B)
SECOND = 1_000_000_000


# --- synthetic runtime telemetry / result fixtures ---------------------------


def _telemetry(base_ns: int, *, captures=(True, False), winner=None) -> list[dict]:
    """One match's telemetry JSONL documents in converter-expected shape."""
    events: list[dict] = []
    clock = [base_ns]

    def ev(kind: str, **fields) -> None:
        clock[0] += SECOND
        events.append({"kind": kind, "monotonic_ns": clock[0], **fields})

    ev("match_started", models=list(MODELS))
    ev("tool_decision", model=MODEL_A, phase="blue", tool="inspect_service", allowed=True)
    ev("deployment_promoted", model=MODEL_A, protected_policy="header", recovery_posture="header")
    ev("tool_decision", model=MODEL_B, phase="blue", tool="deploy_service", allowed=True)
    ev("deployment_promoted", model=MODEL_B, protected_policy="deny", recovery_posture="public")
    ev("blue_finished")
    ev("interview_line", model=MODEL_A, phase="interview", text="I hardened the header checks.")
    ev("interview_line", model=MODEL_B, phase="interview", text="I expect probing to start early.")
    ev("interview_finished")
    ev("tool_decision", model=MODEL_A, phase="red", tool="describe_target_service", allowed=True)
    ev("tool_decision", model=MODEL_B, phase="red", tool="http_request", allowed=True)
    if captures[1]:
        ev("tool_decision", model=MODEL_B, phase="red", tool="submit_flag", allowed=True)
    if captures[0]:
        ev("tool_decision", model=MODEL_A, phase="red", tool="submit_flag", allowed=True)
    ev("match_finished", winner=winner, captures=list(captures))
    ev("teardown", states=["destroyed", "destroyed"])
    return events


def _result(match_id: str, *, winner, reason_code: str, captures,
            tokens_a: int = 5000, tokens_b: int = 3000) -> dict:
    def usage_entry(model: str, total_tokens: int) -> dict:
        return {"model": model, "turns": 2,
                "input_tokens": total_tokens // 2, "output_tokens": total_tokens - total_tokens // 2,
                "cache_read_tokens": 0, "cache_write_tokens": 0,
                "final_text_sha256": "0" * 64}

    return {
        "result": "passed",
        "outcome": "VALID_CAPTURE" if any(captures) else "VALID_NO_CAPTURE",
        "reason_code": reason_code,
        "publication_enabled": False,
        "is_calibration": True,
        "provider": "command_code",
        "models": list(MODELS),
        "match_id": match_id,
        "seed": f"seed-{match_id}",
        "winner": winner,
        "captures": list(captures),
        "defenses": {MODEL_A: {"protected_policy": "header", "recovery_posture": "header"},
                     MODEL_B: {"protected_policy": "deny", "recovery_posture": "public"}},
        "usage": {
            "blue": [usage_entry(MODEL_A, tokens_a // 3), usage_entry(MODEL_B, tokens_b // 3)],
            "interview": [usage_entry(MODEL_A, tokens_a // 3), usage_entry(MODEL_B, tokens_b // 3)],
            "red": [usage_entry(MODEL_A, tokens_a - 2 * (tokens_a // 3)),
                    usage_entry(MODEL_B, tokens_b - 2 * (tokens_b // 3))],
        },
        "calibration_only": True,
    }


def _series_fixtures(*, m3_winner, m3_reason="NO_CAPTURE_AVAILABILITY", m3_captures=(False, False),
                     tokens_a=5000, tokens_b=3000):
    """Three sequential matches: A wins m1, B wins m2, m3 varies."""
    telemetry = [
        _telemetry(0 * SECOND, captures=(True, False), winner=MODEL_A),
        _telemetry(60 * SECOND, captures=(False, True), winner=MODEL_B),
        _telemetry(120 * SECOND, captures=m3_captures, winner=m3_winner),
    ]
    results = [
        _result("ep-v8-m1", winner=MODEL_A, reason_code="SOLE_CAPTURE", captures=(True, False),
                tokens_a=tokens_a, tokens_b=tokens_b),
        _result("ep-v8-m2", winner=MODEL_B, reason_code="SOLE_CAPTURE", captures=(False, True),
                tokens_a=tokens_a, tokens_b=tokens_b),
        _result("ep-v8-m3", winner=m3_winner, reason_code=m3_reason, captures=m3_captures,
                tokens_a=tokens_a, tokens_b=tokens_b),
    ]
    return telemetry, results


class _FailingIntroDrafter:
    """Deterministic stand-in that always falls back to authored intro copy."""

    def draft(self, identities, facts):
        raise RuntimeError("no agent in tests")


class _FixedIntroDrafter:
    """Returns one valid intro line so two pipelines see identical input."""

    def draft(self, identities, facts):
        return [{
            "voice_role": "play_by_play",
            "line_type": "editorial",
            "scene": "model_cards_and_rules",
            "offset_seconds": 2.0,
            "text": f"Welcome: {identities[0]} against {identities[1]}.",
        }]


def _build(telemetry_docs, result_docs, *, series, drafter=None):
    return build_broadcast_artifacts(
        telemetry_docs, result_docs,
        benchmark_data=DEFAULT_BENCHMARK_DATA,
        intro_drafter=drafter or _FailingIntroDrafter(),
        series=series,
    )


# --- pure series winner logic -------------------------------------------------


def test_first_to_two_wins_clinches_a_two_zero_series():
    decision = decide_series_winner(
        [{"models": list(MODELS), "winner": MODEL_A},
         {"models": list(MODELS), "winner": MODEL_A},
         {"models": list(MODELS), "winner": None}],
        [IDENTITY_A, IDENTITY_B])
    assert decision["series_winner"] == IDENTITY_A
    assert decision["decision_basis"] == BASIS_FIRST_TO_TWO
    assert decision["wins"] == {IDENTITY_A: 2, IDENTITY_B: 0}


def test_two_one_series_goes_to_the_side_with_three_wins_recorded():
    decision = decide_series_winner(
        [{"models": list(MODELS), "winner": MODEL_B},
         {"models": list(MODELS), "winner": MODEL_A},
         {"models": list(MODELS), "winner": MODEL_B}],
        [IDENTITY_A, IDENTITY_B])
    assert decision["series_winner"] == IDENTITY_B
    assert decision["decision_basis"] == BASIS_FIRST_TO_TWO


def test_one_one_one_draw_breaks_tie_by_lowest_total_tokens():
    # Same win count (1-1 plus one drawn match); A burned more provider
    # tokens across the series, so B takes the documented tie-break.
    results = [
        {"models": list(MODELS), "winner": MODEL_A,
         "usage": {"blue": [{"model": MODEL_A, "input_tokens": 9000, "output_tokens": 1000}]}},
        {"models": list(MODELS), "winner": MODEL_B,
         "usage": {"red": [{"model": MODEL_B, "input_tokens": 1000, "output_tokens": 500}]}},
        {"models": list(MODELS), "winner": None},
    ]
    decision = decide_series_winner(results, [IDENTITY_A, IDENTITY_B])
    assert decision["decision_basis"] == BASIS_TOKEN_TIEBREAK
    assert decision["series_winner"] == IDENTITY_B
    assert decision["token_totals"][IDENTITY_A] == 10000
    assert decision["token_totals"][IDENTITY_B] == 1500


def test_exact_token_tie_leaves_the_series_unresolved():
    results = [
        {"models": list(MODELS), "winner": MODEL_A,
         "usage": {"blue": [{"model": MODEL_A, "input_tokens": 700}]}},
        {"models": list(MODELS), "winner": MODEL_B,
         "usage": {"blue": [{"model": MODEL_B, "input_tokens": 700}]}},
        {"models": list(MODELS), "winner": None},
    ]
    decision = decide_series_winner(results, [IDENTITY_A, IDENTITY_B])
    assert decision["series_winner"] is None
    assert decision["decision_basis"] == BASIS_UNRESOLVED


def test_identity_total_tokens_sums_every_phase_and_ignores_junk():
    result = _result("m1", winner=MODEL_A, reason_code="SOLE_CAPTURE", captures=(True, False),
                     tokens_a=6000, tokens_b=2000)
    assert identity_total_tokens(result, IDENTITY_A) == 6000
    assert identity_total_tokens(result, IDENTITY_B) == 2000
    noisy = {"usage": {"blue": [{"model": MODEL_A, "input_tokens": "junk", "output_tokens": None}]}}
    assert identity_total_tokens(noisy, IDENTITY_A) == 0
    assert identity_total_tokens({}, IDENTITY_A) == 0


def test_series_summary_record_shape_and_evidence_paths():
    _telemetry_docs, results = _series_fixtures(m3_winner=None)
    summary = build_series_summary(
        "ep-v8", results,
        match_ids=["ep-v8-m1", "ep-v8-m2", "ep-v8-m3"],
        evidence_paths=[{"telemetry": f"/e/ep-v8-m{i}.telemetry.jsonl",
                         "result": f"/e/ep-v8-m{i}.result.json"} for i in (1, 2, 3)])
    assert summary["schema_version"] == SERIES_SCHEMA
    assert summary["protocol"] == "best_of_3"
    assert [entry["match_number"] for entry in summary["matches"]] == [1, 2, 3]
    assert [entry["match_id"] for entry in summary["matches"]] == ["ep-v8-m1", "ep-v8-m2", "ep-v8-m3"]
    assert [entry["winner"] for entry in summary["matches"]] == [IDENTITY_A, IDENTITY_B, None]
    assert summary["matches"][0]["evidence"]["telemetry"] == "/e/ep-v8-m1.telemetry.jsonl"
    assert summary["series_winner"] == IDENTITY_B          # token tie-break
    assert summary["tie_break"]["token_totals"][IDENTITY_A] > summary["tie_break"]["token_totals"][IDENTITY_B]


def test_series_report_outcome_describes_standings_and_basis():
    outcome = series_report_outcome({"series_winner": IDENTITY_A, "decision_basis": BASIS_FIRST_TO_TWO,
                                     "wins": {IDENTITY_A: 2, IDENTITY_B: 1}})
    assert outcome["winner"] == IDENTITY_A
    assert "first to two match wins" in outcome["basis"]


# --- replay merge --------------------------------------------------------------


def _single_match_replays() -> list[dict]:
    telemetry_docs, result_docs = _series_fixtures(m3_winner=None)
    return [convert(telemetry, result)[1].to_dict()
            for telemetry, result in zip(telemetry_docs, result_docs)]


def test_merge_renumbers_matches_event_ids_and_keeps_clock_monotonic():
    replays = _single_match_replays()
    merged = merge_series_replays(replays)
    frames = merged["frames"]
    assert len(frames) == sum(len(replay["frames"]) for replay in replays)
    assert [frame["sequence"] for frame in frames] == list(range(1, len(frames) + 1))
    assert {frame["event_id"].split(":")[0] for frame in frames} == {"m1", "m2", "m3"}
    assert len({frame["event_id"] for frame in frames}) == len(frames)
    numbers = [frame["match_number"] for frame in frames]
    assert set(numbers) == {1, 2, 3}
    assert numbers == sorted(numbers)
    timestamps = [frame["at_monotonic_ns"] for frame in frames]
    assert all(left < right for left, right in zip(timestamps, timestamps[1:])), \
        "merged timeline must be strictly increasing across matches"
    assert len(merged["source_bundle_hash"]) == 64
    assert merged["panes"] == replays[0]["panes"]
    assert merged["schema_version"] == "sandboxer.replay.v1"


def test_merge_offsets_overlapping_match_clocks_defensively():
    # Match 2's telemetry clock starts BEFORE match 1's last event: the merge
    # must still produce a globally increasing timeline without reordering.
    first = convert(_telemetry(0 * SECOND), _result("a-m1", winner=MODEL_A, reason_code="SOLE_CAPTURE",
                                                    captures=(True, False)))[1].to_dict()
    overlapping_base = first["frames"][-1]["at_monotonic_ns"] - 5 * SECOND
    second = convert(_telemetry(overlapping_base, captures=(False, True), winner=MODEL_B),
                     _result("a-m2", winner=MODEL_B, reason_code="SOLE_CAPTURE",
                             captures=(False, True)))[1].to_dict()
    merged = merge_series_replays([first, second])
    timestamps = [frame["at_monotonic_ns"] for frame in merged["frames"]]
    assert all(left < right for left, right in zip(timestamps, timestamps[1:]))
    # Intra-match spacing of the second match survives via a constant offset.
    raw_gap = int(second["frames"][1]["at_monotonic_ns"]) - int(second["frames"][0]["at_monotonic_ns"])
    shifted_index = len(first["frames"])
    merged_gap = timestamps[shifted_index + 1] - timestamps[shifted_index]
    assert merged_gap == raw_gap


def test_merge_rejects_empty_and_foreign_replays():
    with pytest.raises(ArtifactInputError, match="SERIES_REPLAYS_EMPTY"):
        merge_series_replays([])
    with pytest.raises(ArtifactInputError, match="SERIES_REPLAY_SCHEMA_INVALID"):
        merge_series_replays([{"schema_version": "sandboxer.replay.v0", "frames": []}])


# --- combined video manifest ---------------------------------------------------


def _windows(scenes):
    out = []
    cursor = 0
    for scene in scenes:
        out.append((scene, cursor, cursor + int(scene["duration_frames"])))
        cursor += int(scene["duration_frames"])
    return out


@pytest.fixture(scope="module")
def tie_series_artifacts():
    telemetry_docs, result_docs = _series_fixtures(m3_winner=None)
    return _build(telemetry_docs, result_docs, series=True)


def test_series_manifest_has_three_match_blocks_with_intermissions(tie_series_artifacts):
    scenes = tie_series_artifacts["manifest"]["scenes"]
    types = [scene["type"] for scene in scenes]
    expected_block = ["match", "interview_card", "interviews", "red_phase_card", "match"]
    assert types == (["custom_intro", "model_cards_and_rules"]
                     + expected_block + ["intermission"]
                     + expected_block + ["intermission"]
                     + expected_block + ["factual_recap"])
    intermissions = [scene for scene in scenes if scene["type"] == "intermission"]
    assert len(intermissions) == 2
    match_scenes = [scene for scene in scenes if scene["type"] == "match"]
    assert sorted({scene["match_number"] for scene in match_scenes}) == [1, 2, 3]
    keys = [scene.get("scene_key") for scene in scenes if scene.get("scene_key")]
    assert len(keys) == len(set(keys)), "packing keys must stay unique per scene instance"


def test_model_cards_appear_once_before_the_first_match_only(tie_series_artifacts):
    scenes = tie_series_artifacts["manifest"]["scenes"]
    positions = [index for index, scene in enumerate(scenes) if scene["type"] == "model_cards_and_rules"]
    assert positions == [1]
    assert scenes[2]["type"] == "match" and scenes[2]["match_number"] == 1


def test_terminal_offsets_accumulate_across_the_three_matches(tie_series_artifacts):
    terminal = tie_series_artifacts["manifest"]["terminal"]
    assert {entry["event_id"].split(":")[0] for entry in terminal} == {"m1", "m2", "m3"}
    for prefix in ("m1", "m2", "m3"):
        frames_of_match = [entry["at_frame"] for entry in terminal if entry["event_id"].startswith(prefix)]
        assert frames_of_match == sorted(frames_of_match), f"{prefix} events must run forward"
    bounds = {
        prefix: (min(entry["at_frame"] for entry in terminal if entry["event_id"].startswith(prefix)),
                 max(entry["at_frame"] for entry in terminal if entry["event_id"].startswith(prefix)))
        for prefix in ("m1", "m2", "m3")
    }
    assert bounds["m1"][1] < bounds["m2"][0] < bounds["m2"][1] < bounds["m3"][0], \
        "each later match must sit entirely after the previous one on the timeline"


def test_every_terminal_event_lands_inside_exactly_one_scene_window(tie_series_artifacts):
    windows = _windows(tie_series_artifacts["manifest"]["scenes"])
    for entry in tie_series_artifacts["manifest"]["terminal"]:
        owners = [scene for scene, start, end in windows if start <= entry["at_frame"] < end]
        assert len(owners) == 1, f"{entry['event_id']} must fall inside exactly one scene window"
        assert owners[0]["type"] in {"match", "interviews"}, entry


def test_blue_part_events_anchor_into_their_own_match_scene(tie_series_artifacts):
    windows = _windows(tie_series_artifacts["manifest"]["scenes"])
    for entry in tie_series_artifacts["manifest"]["terminal"]:
        if entry["phase"] != "blue" or entry["event_type"] not in {"TOOL_CALL", "MODEL_RESPONSE", "MATCH_STARTED"}:
            continue
        prefix, _, _local_id = entry["event_id"].partition(":")
        owner = next(scene for scene, start, end in windows if start <= entry["at_frame"] < end)
        assert owner["type"] == "match"
        assert owner["match_number"] == int(prefix[1:])
        assert entry["event_id"] in owner.get("event_ids", [])


def test_commentary_is_dense_nonoverlapping_and_spans_all_matches(tie_series_artifacts):
    lines = tie_series_artifacts["manifest"]["commentary"]
    assert lines
    assert all(left["end_frame"] <= right["start_frame"] for left, right in zip(lines, lines[1:]))
    grounded_prefixes = {line["event_ids"][0].split(":")[0]
                         for line in lines if line["event_ids"]}
    assert {"m1", "m2", "m3"} <= grounded_prefixes
    for line in lines:
        if line["event_ids"]:
            prefixes = {event_id.split(":")[0] for event_id in line["event_ids"]}
            assert len(prefixes) == 1, "no commentary line may mix two matches"


def test_interviews_from_all_three_matches_are_present(tie_series_artifacts):
    interviews = tie_series_artifacts["manifest"]["interviews"]
    assert {entry["event_ids"][0].split(":")[0] for entry in interviews} == {"m1", "m2", "m3"}
    interview_scenes = [scene for scene in tie_series_artifacts["manifest"]["scenes"]
                        if scene["type"] == "interviews"]
    assert len(interview_scenes) == 3


def test_recap_shows_the_decided_series_winner(tie_series_artifacts):
    report = tie_series_artifacts["report"]
    recap = tie_series_artifacts["manifest"]["scenes"][-1]
    assert recap["type"] == "factual_recap"
    # 1 win each + a draw -> Muse Spark wins on lower total tokens.
    assert report["outcome"]["winner"] == IDENTITY_B
    assert "tie-break" in report["outcome"]["basis"]
    assert recap["winner"] == IDENTITY_B
    assert recap["outcome_basis"] == report["outcome"]["basis"]
    chapters = report["technical_chapters"]
    assert [chapter["match_number"] for chapter in chapters] == [1, 2, 3]


def test_two_zero_series_recap_names_the_clinching_identity():
    telemetry_docs, result_docs = _series_fixtures(m3_winner=None)
    # A clean 2-0: A also takes what was m2; game three stays drawn footage.
    result_docs[1] = _result("ep-v8-m2", winner=MODEL_A, reason_code="SOLE_CAPTURE",
                             captures=(True, False))
    artifacts = _build(telemetry_docs, result_docs, series=True)
    assert artifacts["report"]["outcome"]["winner"] == IDENTITY_A
    assert artifacts["report"]["outcome"]["basis"].startswith("Best-of-3 series")


def test_series_mode_requires_exactly_three_matches():
    telemetry_docs, result_docs = _series_fixtures(m3_winner=None)
    with pytest.raises(ArtifactInputError, match="ARTIFACT_INPUT_COUNT_INVALID"):
        _build(telemetry_docs[:2], result_docs[:2], series=True)
    with pytest.raises(ArtifactInputError, match="ARTIFACT_INPUT_COUNT_INVALID"):
        _build(telemetry_docs[:2], result_docs[:2], series=False)


# --- backward compatibility ----------------------------------------------------


def test_single_match_pipeline_stays_identical_to_legacy_composition():
    telemetry_docs, result_docs = _series_fixtures(m3_winner=None)
    telemetry_doc, result_doc = telemetry_docs[0], result_docs[0]

    built = _build([telemetry_doc], [result_doc], series=False, drafter=_FixedIntroDrafter())

    # The legacy inline path, composed exactly as main() did before --series.
    legacy_replay = convert(telemetry_doc, result_doc)[1].to_dict()
    legacy_report = build_report(result_doc)
    legacy_arena = draft_arena_plan(legacy_replay, legacy_report, drafter=FakeArenaVisualDrafter())
    identities = [str(pane["identity"]) for pane in legacy_replay["panes"]]
    legacy_intro = draft_intro_commentary(identities, {"models": {}, "benchmark_snapshot": {}},
                                          drafter=_FixedIntroDrafter())
    # Legacy path with the same recap speak-boundary clamp that main() now
    # applies on top of the built manifest (episode-v8d fix).
    legacy_dense = build_commentary(legacy_replay["frames"], identities, fps=30)
    legacy_manifest = build_video_manifest(
        legacy_replay, report=legacy_report,
        model_metadata={
            IDENTITY_A: {"producer": "Poolside", "architecture": "Mixture-of-Experts", "context_length": 128_000},
            IDENTITY_B: {"producer": "Meta", "architecture": "Mixture-of-Experts", "context_length": 128_000},
        },
        benchmark_snapshot=load_benchmark_snapshot(DEFAULT_BENCHMARK_DATA, IDENTITY_A, IDENTITY_B),
        fps=30,
        arena_visuals=legacy_arena,
        intro_commentary=legacy_intro,
        dense_commentary=legacy_dense,
    )
    assert built["replay"] == legacy_replay
    assert built["report"] == legacy_report
    # main() now applies the recap speak-boundary clamp to the manifest's
    # commentary (episode-v8d fix). Reproduce it on the legacy manifest so the
    # comparison covers the full pipeline behaviour, not raw builder output.
    boundary = sum(int(scene["duration_frames"])
                   for scene in legacy_manifest["scenes"][:-1])
    legacy_clamped = [
        line for line in legacy_manifest["commentary"]
        if int(line["start_frame"]) + 30 <= boundary]
    for line in legacy_clamped:
        start = int(line["start_frame"])
        line["end_frame"] = min(int(line["end_frame"]), max(boundary - 1, start + 30))
    assert built["manifest"]["commentary"] == legacy_clamped
    manifest_rest = {k: v for k, v in built["manifest"].items() if k != "commentary"}
    legacy_manifest_rest = {k: v for k, v in legacy_manifest.items() if k != "commentary"}
    assert manifest_rest == legacy_manifest_rest
    assert built["series"] is None
    # ...and keeps the historical scene shape: one match, no intermissions.
    types = [scene["type"] for scene in built["manifest"]["scenes"]]
    assert types == ["custom_intro", "model_cards_and_rules", "match", "interview_card",
                     "interviews", "red_phase_card", "match", "factual_recap"]
    assert all(frame["match_number"] == 1 for frame in built["replay"]["frames"])


# --- run_series driver (injected runner; no QEMU) --------------------------------


class _ScriptedRunner:
    """Returns scripted winners or raises scripted failures, in call order."""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[argparse.Namespace] = []

    def __call__(self, args):
        self.calls.append(args)
        step = self.script[len(self.calls) - 1]
        if isinstance(step, BaseException):
            raise step
        return {
            "result": "passed",
            "outcome": "VALID_CAPTURE" if step else "VALID_NO_CAPTURE",
            "reason_code": "SOLE_CAPTURE" if step else "NO_CAPTURE_AVAILABILITY",
            "models": list(args.models),
            "match_id": args.match_id,
            "seed": args.seed,
            "winner": step,
            "captures": [step == MODELS[0], step == MODELS[1]],
            "usage": {},
        }


def _series_args(evidence_dir, **overrides):
    base = dict(
        image="/tmp/opencode/never-used.qcow2",
        profile="/tmp/opencode/never-used.profile.json",
        series_id="ep-v8",
        seed_prefix=None,
        models=list(MODELS),
        runner_root="/tmp/opencode/runners",
        evidence_dir=evidence_dir,
        ttl_seconds=600,
        phase_timeout=180.0,
        blue_tokens=4096,
        red_tokens=4096,
        interview_tokens=1024,
        blue_turns=4,
        red_turns=5,
        blue_tools=8,
        red_tools=10,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def test_execute_series_plays_three_matches_with_suffixed_ids_and_seeds(tmp_path):
    runner = _ScriptedRunner([MODEL_A, MODEL_A, None])
    summary = execute_series(_series_args(tmp_path), runner=runner)
    assert SERIES_MATCHES == 3
    assert len(runner.calls) == 3
    assert [call.match_id for call in runner.calls] == ["ep-v8-m1", "ep-v8-m2", "ep-v8-m3"]
    assert [call.seed for call in runner.calls] == ["ep-v8-m1", "ep-v8-m2", "ep-v8-m3"]
    assert all(list(call.models) == list(MODELS) for call in runner.calls)
    assert summary["wins"] == {IDENTITY_A: 2, IDENTITY_B: 0}
    assert summary["series_winner"] == IDENTITY_A
    assert summary["decision_basis"] == BASIS_FIRST_TO_TWO
    record = tmp_path / "ep-v8.series.json"
    written = json.loads(record.read_text(encoding="utf-8"))
    assert written["schema_version"] == SERIES_SCHEMA
    assert [entry["match_id"] for entry in written["matches"]] == ["ep-v8-m1", "ep-v8-m2", "ep-v8-m3"]
    assert written["matches"][0]["evidence"]["telemetry"].endswith("ep-v8-m1.telemetry.jsonl")
    assert written["matches"][2]["winner"] is None
    assert summary["series_record_path"] == str(record)


def test_execute_series_seed_prefix_diverges_match_seeds(tmp_path):
    runner = _ScriptedRunner([MODEL_A, MODEL_B, MODEL_A])
    execute_series(_series_args(tmp_path, seed_prefix="brief-seed"), runner=runner)
    assert [call.seed for call in runner.calls] == ["brief-seed-m1", "brief-seed-m2", "brief-seed-m3"]


def test_execute_series_records_failed_match_and_keeps_playing(tmp_path):
    runner = _ScriptedRunner([
        MatchCalibrationError("RUNNER_PREFLIGHT_FAILED"),
        MODEL_A,
        MODEL_A,
    ])
    summary = execute_series(_series_args(tmp_path), runner=runner)
    assert len(runner.calls) == 3, "one failed match must not abort the series"
    failed = summary["matches"][0]
    assert failed["result_status"] == "failed"
    assert failed["reason_code"] == "RUNNER_PREFLIGHT_FAILED"
    assert failed["winner"] is None
    assert summary["matches"][1]["result_status"] == "passed"
    assert summary["series_winner"] == IDENTITY_A


def test_per_match_args_override_only_match_identity_fields(tmp_path):
    args = _series_args(tmp_path)
    match_args = _per_match_args(args, 2)
    assert match_args.match_id == "ep-v8-m2"
    assert match_args.seed == "ep-v8-m2"
    assert match_args.image == args.image and match_args.profile == args.profile
    assert match_args.blue_tokens == args.blue_tokens and match_args.red_turns == args.red_turns
    assert match_args.evidence_dir == args.evidence_dir


def test_run_one_match_is_the_importable_sync_core():
    assert callable(run_one_match)
    assert run_one_match.__doc__ and "run_one_match" in run_one_match.__doc__
