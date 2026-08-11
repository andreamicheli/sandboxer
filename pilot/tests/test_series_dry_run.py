from __future__ import annotations

import json
import subprocess
import sys

from sandboxer_v0 import (
    ControlledClock,
    ControlledCompetitor,
    FakeModelAdapter,
    FakeRunnerBackend,
    MatchPolicy,
    SeriesSpec,
    execute_series,
)


def _spec(
    *,
    beta_response: str = "finish",
    runner_teardown: str = "destroy",
    atlas_tokens: int = 25,
    final_health: tuple[tuple[str, bool], ...] = (),
) -> SeriesSpec:
    return SeriesSpec(
        schema_version="sandboxer.series-spec.v1",
        series_id="dry-run-001",
        seed="calibration-seed",
        competitors=(
            ControlledCompetitor(
                "atlas",
                FakeModelAdapter("atlas", ("defend", "capture"), output_tokens_per_response=atlas_tokens),
            ),
            ControlledCompetitor("borealis", FakeModelAdapter("borealis", ("defend", beta_response))),
        ),
        match_policy=MatchPolicy(best_of=3, output_token_budget=100, turn_budget=2, tool_budget=2),
        runner_backend=FakeRunnerBackend(teardown=runner_teardown, final_health=final_health),
        clock=ControlledClock(
            base_utc="2026-08-11T12:00:00+00:00",
            start_monotonic_ns=5_000_000_000,
            step_ns=250_000_000,
        ),
    )


def test_valid_series_spec_produces_a_deterministic_publishable_release_bundle() -> None:
    first = execute_series(_spec())
    second = execute_series(_spec())

    assert first.publication_eligible is True
    assert first.series_winner == "atlas"
    assert first.matches_completed == 2
    assert first.artifact_manifest["telemetry"] == first.telemetry_hash
    assert first.artifact_manifest["release_bundle"] == first.bundle_hash
    assert first.replay["schema"] == "sandboxer.replay.v1"
    assert first.report["disclaimer"] == "experimental benchmark in a simulated CTF Arena"
    assert first.broadcast_manifest["source_telemetry"] == first.telemetry_hash
    assert first == second
    assert {event["evidence_kind"] for event in first.telemetry} >= {
        "MODEL_CLAIMED",
        "RUNNER_OBSERVED",
        "ORCHESTRATOR_VERIFIED",
    }
    assert all(match["teardown"] == "destroyed" for match in first.match_results)


def test_teardown_uncertainty_is_a_fail_closed_terminal_outcome_with_evidence() -> None:
    outcome = execute_series(_spec(runner_teardown="uncertain"))

    assert outcome.publication_eligible is False
    assert outcome.terminal_code == "TEARDOWN_UNCERTAIN"
    assert outcome.series_winner is None
    assert outcome.quarantined_runners == ("atlas-runner-1", "borealis-runner-1")
    assert outcome.telemetry[-1]["event_type"] == "SERIES_TERMINATED"
    assert outcome.telemetry[-1]["reason_code"] == "TEARDOWN_UNCERTAIN"


def test_budget_exhaustion_fails_closed_and_preserves_cleanup_evidence() -> None:
    spec = _spec(beta_response="tool tool tool")
    outcome = execute_series(spec)

    assert outcome.publication_eligible is False
    assert outcome.terminal_code == "COMPETITIVE_BUDGET_EXHAUSTED"
    assert any(event["event_type"] == "BUDGET_EXHAUSTED" for event in outcome.telemetry)
    assert any(
        event["event_type"] == "RUNNER_TEARDOWN" and event["status"] == "destroyed"
        for event in outcome.telemetry
    )
    assert outcome.calibration["protocol_change_authorized"] is False
    assert "BUDGET_EXHAUSTION_OBSERVED" in outcome.calibration["flags"]


def test_public_command_executes_the_controlled_fixture() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "sandboxer_v0", "--fixture", "valid"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["publication_eligible"] is True
    checks = {check["requirement_id"]: check for check in payload["calibration"]["requirements"]}
    assert checks["REQ-COMMAND-CODE-CREDITS"]["status"] == "not-evaluable"


def test_public_credit_pressure_fixture_uses_an_explicit_simulated_allowance() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "sandboxer_v0", "--fixture", "credit-pressure"],
        text=True,
        capture_output=True,
        check=False,
    )

    payload = json.loads(result.stdout)
    assert result.returncode == 0
    assert payload["calibration"]["command_code_credit_pressure"]["allowance"] == 10
    assert "CREDIT_PRESSURE" in payload["calibration"]["flags"]


def test_optional_state_record_makes_the_same_fixture_idempotently_observable(tmp_path) -> None:
    state_path = tmp_path / "series-state.json"
    first = execute_series(_spec(), state_path=state_path)
    second = execute_series(_spec(), state_path=state_path)

    state = json.loads(state_path.read_text())
    assert first.bundle_hash == second.bundle_hash == state["terminal"]["bundle_hash"]
    assert state["transitions"][-1]["state"] == "REVIEW"
    assert [transition["sequence"] for transition in state["transitions"]] == list(
        range(1, len(state["transitions"]) + 1)
    )


def test_normalized_telemetry_has_complete_deterministic_envelopes() -> None:
    first = execute_series(_spec())
    second = execute_series(_spec())

    required = {
        "schema_version",
        "series_id",
        "match_id",
        "event_id",
        "event_type",
        "wall_time_utc",
        "orchestrator_monotonic_ns",
        "emitter",
        "phase",
        "turn",
        "causal_parent_id",
        "correlation_id",
        "idempotency_key",
        "redaction_class",
        "source_adapter",
        "component_versions",
        "previous_event_hash",
        "event_hash",
    }
    assert all(required <= event.keys() for event in first.telemetry)
    assert first.telemetry == second.telemetry
    assert first.telemetry[1]["orchestrator_monotonic_ns"] - first.telemetry[0]["orchestrator_monotonic_ns"] == 250_000_000
    assert all(event["match_id"] for event in first.telemetry if event.get("match_number"))


def test_outcome_first_scoring_uses_submission_order_only_after_health_and_fails_closed_on_exact_ties() -> None:
    dual = execute_series(_spec(beta_response="capture"))
    none_spec = SeriesSpec(
        **{
            **_spec().__dict__,
            "competitors": (
                ControlledCompetitor("atlas", FakeModelAdapter("atlas", ("defend", "finish"))),
                ControlledCompetitor("borealis", FakeModelAdapter("borealis", ("defend", "finish"))),
            ),
        }
    )
    none = execute_series(none_spec)

    assert dual.terminal_code == "SERIES_COMPLETED"
    assert dual.series_winner is not None
    assert all(match["reason_code"] == "DUAL_CAPTURE_SUBMISSION_ORDER" for match in dual.match_results)
    assert none.terminal_code == "EXACT_TIE_UNRESOLVED"
    assert none.series_winner is None
    assert not none.publication_eligible


def test_output_token_budget_is_enforced_at_the_fake_adapter_boundary() -> None:
    outcome = execute_series(_spec(atlas_tokens=101))

    assert outcome.terminal_code == "COMPETITIVE_BUDGET_EXHAUSTED"
    exhausted = [event for event in outcome.telemetry if event["event_type"] == "BUDGET_EXHAUSTED"]
    assert exhausted[0]["budget_kind"] == "output_tokens"


def test_release_calibration_contains_evidence_linked_requirement_checks() -> None:
    outcome = execute_series(_spec())

    checks = outcome.calibration["requirements"]
    assert {check["requirement_id"] for check in checks} >= {
        "REQ-MATCH-TERMINAL",
        "REQ-BLUE-BRIEF-VARIETY",
        "REQ-COMMAND-CODE-CREDITS",
    }
    assert all(check["status"] in {"satisfied", "unmet", "not-evaluable"} for check in checks)
    assert all(check["evidence_event_ids"] for check in checks)
    assert outcome.calibration["requirement_summary"]["counts"]["unmet"] == 0
    assert outcome.calibration["protocol_change_authorized"] is False


def test_series_does_not_infer_provider_credits_from_competitive_budget() -> None:
    outcome = execute_series(_spec())
    checks = {check["requirement_id"]: check for check in outcome.calibration["requirements"]}

    assert outcome.calibration["command_code_credit_pressure"]["pressure"] == "unknown"
    assert checks["REQ-COMMAND-CODE-CREDITS"]["status"] == "not-evaluable"


def test_series_can_declare_a_simulated_provider_allowance_for_high_pressure_diagnostics() -> None:
    values = {**_spec().__dict__, "command_code_credit_allowance": 10}
    outcome = execute_series(SeriesSpec(**values))

    assert outcome.calibration["command_code_credit_pressure"]["pressure"] == "high"
    assert "CREDIT_PRESSURE" in outcome.calibration["flags"]


def test_canonical_protocol_plays_a_first_to_two_series_with_recomputable_score_proof() -> None:
    spec = SeriesSpec(
        **{
            **_spec().__dict__,
            "competitors": (
                ControlledCompetitor(
                    "atlas",
                    FakeModelAdapter(
                        "atlas",
                        ("defend", "capture"),
                        match_responses=(("defend", "capture"), ("defend", "finish"), ("defend", "capture")),
                    ),
                ),
                ControlledCompetitor(
                    "borealis",
                    FakeModelAdapter(
                        "borealis",
                        ("defend", "finish"),
                        match_responses=(("defend", "finish"), ("defend", "capture"), ("defend", "finish")),
                    ),
                ),
            ),
        }
    )

    outcome = execute_series(spec)

    assert outcome.publication_eligible is True
    assert outcome.terminal_code == "SERIES_COMPLETED"
    assert outcome.series_winner == "atlas"
    assert outcome.matches_completed == 3
    assert [match["winner"] for match in outcome.match_results] == ["atlas", "borealis", "atlas"]
    assert outcome.match_results[2]["roles"][0] == "atlas"  # Match 2 loser begins Match 3.
    assert outcome.report["score_proof"]["wins"] == {"atlas": 2, "borealis": 1}
    verified = {event["event_id"] for event in outcome.telemetry if event["event_type"] == "SUBMISSION_VERIFIED"}
    recomputed_wins = {name: 0 for name in ("atlas", "borealis")}
    for event in outcome.telemetry:
        if event["event_type"] == "MATCH_FINISHED" and event.get("winner") is not None:
            recomputed_wins[event["winner"]] += 1
    assert outcome.report["score_proof"]["wins"] == recomputed_wins
    assert all(
        set(match["verified_submission_event_ids"]) <= verified
        for match in outcome.report["score_proof"]["matches"]
    )
    for submission in (event for event in outcome.telemetry if event["event_type"] == "SUBMISSION_VERIFIED"):
        claim = next(event for event in outcome.telemetry if event["event_id"] == submission["claimed_response_event_id"])
        assert claim["evidence_kind"] == "MODEL_CLAIMED"
        assert "capture" in claim["response"]


def test_canonical_protocol_rotates_roles_without_public_internal_aliases() -> None:
    outcome = execute_series(_spec())

    first, second = outcome.match_results
    assert second["roles"] == tuple(reversed(first["roles"]))
    public_values = repr(outcome.match_results) + repr(outcome.telemetry)
    assert "Alpha" not in public_values
    assert "Beta" not in public_values


def test_each_match_has_concurrent_phase_gates_and_an_isolated_interview() -> None:
    outcome = execute_series(_spec())

    for number in range(1, outcome.matches_completed + 1):
        events = [event for event in outcome.telemetry if event.get("match_number") == number]
        gates = [event for event in events if event["event_type"] == "PHASE_GATE_OPENED"]
        assert [event["phase"] for event in gates] == ["blue", "red"]
        assert all(event["execution_mode"] == "coordinated_rounds" for event in gates)
        assert all("concurrent" not in event for event in gates)
        assert all(event["peer_response_visible"] is False for event in events if event["event_type"] == "MODEL_RESPONSE")
        interview = [event for event in events if event["event_type"] == "INTERVIEW_RECORDED"]
        assert len(interview) == 2
        assert all(event["tool_access"] is False for event in interview)
        assert all(event["opponent_context_included"] is False for event in interview)
        assert not [event for event in events if event["phase"] == "interview" and event["event_type"] == "BUDGET_OBSERVED"]
        blue_responses = [event for event in events if event["event_type"] == "MODEL_RESPONSE" and event["phase"] == "blue"]
        red_responses = [event for event in events if event["event_type"] == "MODEL_RESPONSE" and event["phase"] == "red"]
        assert {event["competitor"] for event in blue_responses} == {"atlas", "borealis"}
        assert {event["competitor"] for event in red_responses} == {"atlas", "borealis"}


def test_both_capture_and_no_capture_follow_outcome_first_scoring_rules() -> None:
    both_capture = execute_series(_spec(beta_response="capture", final_health=(("borealis", False),)))
    no_capture = execute_series(
        SeriesSpec(
            **{
                **_spec().__dict__,
                "competitors": (
                    ControlledCompetitor("atlas", FakeModelAdapter("atlas", ("defend", "finish"))),
                    ControlledCompetitor("borealis", FakeModelAdapter("borealis", ("defend", "finish"))),
                ),
                "runner_backend": FakeRunnerBackend(final_health=(("borealis", False),)),
            }
        )
    )

    assert both_capture.publication_eligible is True
    assert both_capture.series_winner == "atlas"
    assert all(match["reason_code"] == "DUAL_CAPTURE_HEALTH" for match in both_capture.match_results)
    assert all(
        event["healthy"] is False
        for event in both_capture.telemetry
        if event["event_type"] == "RUNNER_HEALTH" and event["phase"] == "finalizing" and event["competitor"] == "borealis"
    )
    assert no_capture.publication_eligible is True
    assert no_capture.series_winner == "atlas"
    assert all(match["reason_code"] == "NO_CAPTURE_AVAILABILITY" for match in no_capture.match_results)


def test_blue_rounds_collect_peer_responses_before_observing_budget_and_enforce_turns_symmetrically() -> None:
    blue_loop = SeriesSpec(
        **{
            **_spec().__dict__,
            "competitors": (
                ControlledCompetitor("atlas", FakeModelAdapter("atlas", ("continue", "capture"))),
                ControlledCompetitor("borealis", FakeModelAdapter("borealis", ("continue", "finish"))),
            ),
            "match_policy": MatchPolicy(best_of=3, output_token_budget=100, turn_budget=1, tool_budget=2),
        }
    )

    outcome = execute_series(blue_loop)

    assert outcome.terminal_code == "COMPETITIVE_BUDGET_EXHAUSTED"
    exhausted = [event for event in outcome.telemetry if event["event_type"] == "BUDGET_EXHAUSTED"]
    assert exhausted[0]["phase"] == "blue"
    assert exhausted[0]["budget_kind"] == "turns"
    first_round_claims = [
        event for event in outcome.telemetry
        if event["event_type"] == "MODEL_RESPONSE" and event["phase"] == "blue" and event["turn"] == 1
    ]
    first_round_budget = [
        event for event in outcome.telemetry
        if event["event_type"] == "BUDGET_OBSERVED" and event["phase"] == "blue" and event["turn"] == 1
    ]
    assert len(first_round_claims) == len(first_round_budget) == 2
    assert max(outcome.telemetry.index(event) for event in first_round_claims) < min(outcome.telemetry.index(event) for event in first_round_budget)


def test_response_boundary_turn_budget_and_elapsed_backstop_terminate_safely() -> None:
    turn_limited = SeriesSpec(
        **{
            **_spec().__dict__,
            "competitors": (
                ControlledCompetitor("atlas", FakeModelAdapter("atlas", ("defend", "continue"))),
                ControlledCompetitor("borealis", FakeModelAdapter("borealis", ("defend", "finish"))),
            ),
            "match_policy": MatchPolicy(best_of=3, output_token_budget=100, turn_budget=1, tool_budget=2),
        }
    )
    timeout_limited = SeriesSpec(
        **{
            **_spec().__dict__,
            "match_policy": MatchPolicy(
                best_of=3,
                output_token_budget=100,
                turn_budget=2,
                tool_budget=2,
                elapsed_time_backstop_seconds=0.01,
            ),
        }
    )

    exhausted = execute_series(turn_limited)
    timed_out = execute_series(timeout_limited)

    assert exhausted.terminal_code == "COMPETITIVE_BUDGET_EXHAUSTED"
    assert {event["budget_kind"] for event in exhausted.telemetry if event["event_type"] == "BUDGET_EXHAUSTED"} == {"turns"}
    assert timed_out.terminal_code == "MATCH_TIMEOUT"
    assert all(match["teardown"] == "destroyed" for match in timed_out.match_results)
