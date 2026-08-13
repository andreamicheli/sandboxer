import pytest

from sandboxer_v0.calibration import diagnose_dry_run


def test_diagnosis_reports_duration_density_and_terminal_reason() -> None:
    events = [
        {"event_id": "e-start", "event": "match_started", "monotonic_seconds": 10},
        {"event_id": "e-blue", "event": "blue_action", "monotonic_seconds": 11},
        {"event_id": "e-red", "event": "red_action", "monotonic_seconds": 14, "captured": True},
        {"event_id": "e-finish", "event": "match_finished", "monotonic_seconds": 20, "terminal_reason": "winner_decided"},
    ]

    result = diagnose_dry_run(events, blue_briefs=["Find the exposed route", "Find the exposed route"])

    assert result["match"]["duration_seconds"] == 10
    assert result["match"]["action_count"] == 2
    assert result["match"]["actions_per_minute"] == 12.0
    assert result["terminal"]["reason"] == "winner_decided"
    assert "BLUE_BRIEF_REPETITION" in result["flags"]


def test_diagnosis_projects_credit_pressure_and_recommends_calibration() -> None:
    events = [
        {"event": "match_started", "monotonic_seconds": 0},
        {"event": "model_turn", "monotonic_seconds": 1, "output_tokens": 900, "tool_calls": 3},
        {"event": "model_turn", "monotonic_seconds": 2, "output_tokens": 800, "tool_calls": 2},
        {"event": "match_finished", "monotonic_seconds": 3, "terminal_reason": "budget_exhausted", "failure_code": "TURN_BUDGET"},
    ]

    result = diagnose_dry_run(
        events,
        config={"red_output_token_budget": 2048, "red_max_turns": 4},
        provider_credit_allowance=3_000,
    )

    assert result["usage"] == {"output_tokens": 1_700, "turns": 2, "tool_calls": 5}
    assert result["credits"]["projected_output_tokens"] == 3_400
    assert result["credits"]["pressure"] == "high"
    assert result["terminal"]["failure_code"] == "TURN_BUDGET"
    assert "CREDIT_PRESSURE" in result["flags"]
    assert result["recommendations"]


def test_diverse_briefs_are_not_flagged_and_missing_terminal_is_explicit() -> None:
    result = diagnose_dry_run(
        [{"event": "match_started", "monotonic_seconds": 4}],
        blue_briefs=["Patch the parser", "Harden the auth boundary", "Trace the export path"],
    )

    assert result["briefs"]["unique_count"] == 3
    assert result["briefs"]["diversity"] == 1.0
    assert result["terminal"]["reason"] == "unknown"
    assert "MISSING_TERMINAL" in result["flags"]


def test_structured_requirements_have_stable_checks_and_event_evidence() -> None:
    result = diagnose_dry_run([
        {"event_id": "start-1", "event": "match_started", "monotonic_ns": 1_000_000_000},
        {"event_id": "finish-1", "event": "match_finished", "monotonic_ns": 3_000_000_000, "terminal_reason": "winner_decided"},
    ], blue_briefs=["one", "two"])

    checks = {item["requirement_id"]: item for item in result["requirements"]}
    assert checks["REQ-MATCH-TERMINAL"]["check_id"] == "CHECK-MATCH-TERMINAL-V1"
    assert checks["REQ-MATCH-TERMINAL"]["status"] == "satisfied"
    assert checks["REQ-MATCH-TERMINAL"]["evidence_event_ids"] == ["finish-1"]
    assert result["match"]["duration_seconds"] == 2
    assert result["requirement_summary"]["counts"]["satisfied"] >= 1
    assert result["protocol_change_authorized"] is False


def test_requirement_status_can_be_not_evaluable_without_telemetry() -> None:
    result = diagnose_dry_run([], blue_briefs=[])

    checks = {item["requirement_id"]: item for item in result["requirements"]}
    assert checks["REQ-BLUE-BRIEF-VARIETY"]["status"] == "not-evaluable"
    assert checks["REQ-COMMAND-CODE-CREDITS"]["status"] == "not-evaluable"
    assert checks["REQ-MATCH-DURATION"]["status"] == "not-evaluable"
    assert result["protocol_change_authorized"] is False


@pytest.mark.parametrize(
    ("duration", "briefs", "allowance", "minimum", "expected_flag"),
    [
        (2, ["one", "two"], None, 5, "MATCH_TOO_SHORT"),
        (10, ["same", "same"], None, None, "BLUE_BRIEF_REPETITION"),
        (10, ["one", "two"], 100, None, "CREDIT_PRESSURE"),
        (10, ["one", "two"], 10_000, 5, None),
    ],
)
def test_diagnostic_matrix(
    duration: int,
    briefs: list[str],
    allowance: int | None,
    minimum: int | None,
    expected_flag: str | None,
) -> None:
    events = [
        {"event_id": "start", "event": "match_started", "monotonic_seconds": 0},
        {"event_id": "turn", "event": "model_turn", "monotonic_seconds": 1, "output_tokens": 200},
        {"event_id": "finish", "event": "match_finished", "monotonic_seconds": duration, "terminal_reason": "winner_decided"},
    ]
    result = diagnose_dry_run(
        events,
        blue_briefs=briefs,
        config={"red_max_turns": 2},
        provider_credit_allowance=allowance,
        minimum_simulated_duration_seconds=minimum,
    )

    if expected_flag is None:
        assert not {"MATCH_TOO_SHORT", "BLUE_BRIEF_REPETITION", "CREDIT_PRESSURE"} & set(result["flags"])
    else:
        assert expected_flag in result["flags"]
    assert result["protocol_change_authorized"] is False


def test_too_short_requirement_has_stable_id_evidence_and_recommendation() -> None:
    result = diagnose_dry_run(
        [
            {"event_id": "start", "event": "match_started", "monotonic_ns": 0},
            {"event_id": "finish", "event": "match_finished", "monotonic_ns": 2_000_000_000},
        ],
        minimum_simulated_duration_seconds=5,
    )

    check = next(item for item in result["requirements"] if item["requirement_id"] == "REQ-MATCH-DURATION")
    assert check == {
        "requirement_id": "REQ-MATCH-DURATION",
        "check_id": "CHECK-MATCH-DURATION-V1",
        "status": "unmet",
        "evidence_event_ids": ["start", "finish"],
        "detail": "simulated duration 2s is below declared minimum 5s",
    }
    assert "MATCH_TOO_SHORT" in result["flags"]
    assert any("duration" in recommendation.lower() for recommendation in result["recommendations"])

from sandboxer_v0.calibration import diagnose_calibration_sample


def test_diagnosis_detects_defense_policy_repetition_with_limited_sample_wording() -> None:
    events = [
        {"event_id": "start", "event": "match_started", "monotonic_seconds": 0},
        {"event_id": "dep1", "event": "deployment_promoted", "model": "laguna", "protected_policy": "deny", "graph_hash": "g1"},
        {"event_id": "dep2", "event": "deployment_promoted", "model": "muse", "protected_policy": "deny", "graph_hash": "g2"},
        {"event_id": "finish", "event": "match_finished", "monotonic_seconds": 10, "terminal_reason": "winner_decided"},
    ]

    result = diagnose_dry_run(events, blue_briefs=["portable_notes", "shared_notes"])

    assert result["defenses"]["policies"]["unique_count"] == 1
    assert result["defenses"]["policies"]["count"] == 2
    assert "DEFENSE_POLICY_REPETITION" in result["flags"]
    assert result["retune_required"] is True
    assert result["verdict"] == "RETUNE_REQUIRED"

    check = next(item for item in result["requirements"] if item["requirement_id"] == "REQ-DEFENSE-POLICY-DIVERSITY")
    assert check["status"] == "unmet"
    assert "limited-sample" in check["detail"].lower()
    assert check["evidence_event_ids"] == ["dep1", "dep2"]

    rec = next(r for r in result["recommendations"] if "defense policy" in r.lower())
    assert "limited-sample" in rec.lower()
    assert "retune" in rec.lower()


def test_diagnosis_detects_deployment_graph_repetition_with_limited_sample_wording() -> None:
    result = diagnose_dry_run(
        [
            {"event_id": "start", "event": "match_started", "monotonic_seconds": 0},
            {"event_id": "dep1", "kind": "deployment_promoted", "model": "laguna", "protected_policy": "deny", "graph_hash": "g-same"},
            {"event_id": "dep2", "kind": "deployment_promoted", "model": "muse", "protected_policy": "header", "graph_hash": "g-same"},
            {"event_id": "finish", "event": "match_finished", "monotonic_seconds": 10, "terminal_reason": "winner_decided"},
        ],
        blue_briefs=["portable_notes", "shared_notes"],
    )

    assert result["defenses"]["graphs"]["unique_count"] == 1
    assert result["defenses"]["graphs"]["count"] == 2
    assert "DEPLOYMENT_GRAPH_REPETITION" in result["flags"]
    assert result["retune_required"] is True
    assert result["verdict"] == "RETUNE_REQUIRED"

    check = next(item for item in result["requirements"] if item["requirement_id"] == "REQ-DEPLOYMENT-GRAPH-DIVERSITY")
    assert check["status"] == "unmet"
    assert "limited-sample" in check["detail"].lower()
    assert check["evidence_event_ids"] == ["dep1", "dep2"]


def test_diverse_defense_policies_and_graphs_satisfy_requirements() -> None:
    result = diagnose_dry_run(
        [
            {"event_id": "start", "event": "match_started", "monotonic_seconds": 0},
            {"event_id": "finish", "event": "match_finished", "monotonic_seconds": 10, "terminal_reason": "winner_decided"},
        ],
        blue_briefs=["b1", "b2", "b3"],
        defense_policies=["deny", "header", "public"],
        deployment_graphs=["ghash1", "ghash2", "ghash3"],
    )

    assert result["defenses"]["policies"]["unique_count"] == 3
    assert result["defenses"]["graphs"]["unique_count"] == 3
    assert not {"DEFENSE_POLICY_REPETITION", "DEPLOYMENT_GRAPH_REPETITION"} & set(result["flags"])

    checks = {item["requirement_id"]: item for item in result["requirements"]}
    assert checks["REQ-DEFENSE-POLICY-DIVERSITY"]["status"] == "satisfied"
    assert checks["REQ-DEPLOYMENT-GRAPH-DIVERSITY"]["status"] == "satisfied"


def test_diagnose_calibration_sample_returns_retune_required_for_r63_scenario() -> None:
    # Simulating the private r63 calibration where both models chose protected_policy=deny
    sample = [
        {
            "match_id": "r63-cal-1",
            "blue_brief": {"family": "portable_notes"},
            "defenses": {
                "laguna": {"protected_policy": "deny", "graph_hash": "graph-laguna-1"},
                "muse": {"protected_policy": "deny", "graph_hash": "graph-muse-1"},
            },
            "events": [
                {"event_id": "start", "event": "match_started", "monotonic_seconds": 0},
                {"event_id": "turn1", "event": "model_turn", "monotonic_seconds": 1, "output_tokens": 4096, "tool_calls": 20},
                {"event_id": "finish", "event": "match_finished", "monotonic_seconds": 10, "terminal_reason": "budget_exhausted"},
            ],
        }
    ]

    result = diagnose_calibration_sample(sample)
    assert result["sample_size"] == 1
    assert result["defenses"]["policies"]["unique_count"] == 1
    assert result["defenses"]["policies"]["count"] == 2
    assert "DEFENSE_POLICY_REPETITION" in result["flags"]
    assert result["retune_required"] is True
    assert result["verdict"] == "RETUNE_REQUIRED"
    assert any("limited-sample" in rec.lower() for rec in result["recommendations"])
