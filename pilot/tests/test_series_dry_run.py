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
        runner_backend=FakeRunnerBackend(teardown=runner_teardown),
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


def test_deferred_scoring_paths_fail_closed_instead_of_awarding_role_order() -> None:
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

    assert dual.terminal_code == "SCORING_RULE_DEFERRED_DUAL_CAPTURE"
    assert none.terminal_code == "SCORING_RULE_DEFERRED_NO_CAPTURE"
    assert dual.series_winner is none.series_winner is None
    assert not dual.publication_eligible and not none.publication_eligible


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
