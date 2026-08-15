from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

import pytest

from sandboxer_v0 import (
    ArenaConfiguration,
    ARENA_CONFIG_SCHEMA_VERSION,
    ControlledClock,
    ControlledCompetitor,
    FakeModelAdapter,
    FakeRunnerBackend,
    MatchOutcome,
    MatchPolicy,
    MCP_BRIDGE_VERSION,
    MCP_TOOL_SCHEMA_VERSION,
    ReleaseBundle,
    ResultReportError,
    RUNNER_BASE_IMAGE_DIGEST,
    RUNNER_IMAGE_VERSION,
    SeriesSpec,
    build_result_report,
    execute_series,
    resolve_match_outcome,
)
from sandboxer_v0.blue_briefs import BLUE_BRIEF_CATALOG_VERSION, select_blue_briefs, validate_blue_brief
from sandboxer_v0.calibration import diagnose_dry_run
from sandboxer_v0.service_spec import SERVICE_SPEC_VERSION, ServiceSpec


def _make_spec(
    *,
    series_id: str = "freeze-series-001",
    seed: str = "freeze-seed-alpha",
    beta_response: str = "finish",
    runner_teardown: str = "destroy",
    is_calibration: bool = False,
    publication_enabled: bool = True,
    tool_budget: int = 2,
    turn_budget: int = 2,
    token_budget: int = 100,
) -> SeriesSpec:
    return SeriesSpec(
        schema_version="sandboxer.series-spec.v1",
        series_id=series_id,
        seed=seed,
        competitors=(
            ControlledCompetitor("atlas", FakeModelAdapter("atlas", ("defend", "capture"))),
            ControlledCompetitor("borealis", FakeModelAdapter("borealis", ("defend", beta_response))),
        ),
        match_policy=MatchPolicy(
            best_of=3,
            output_token_budget=token_budget,
            turn_budget=turn_budget,
            tool_budget=tool_budget,
        ),
        runner_backend=FakeRunnerBackend(teardown=runner_teardown),
        clock=ControlledClock(
            base_utc="2026-08-13T12:00:00+00:00",
            start_monotonic_ns=1_000_000_000,
            step_ns=250_000_000,
        ),
        is_calibration=is_calibration,
        publication_enabled=publication_enabled,
    )


# ---------------------------------------------------------------------------
# 1. Arena Configuration Versioning & Digest
# ---------------------------------------------------------------------------


def test_arena_configuration_versioning_and_digest() -> None:
    config = ArenaConfiguration(
        runner_image=RUNNER_IMAGE_VERSION,
        runner_base_image_digest=RUNNER_BASE_IMAGE_DIGEST,
        mcp_bridge_version=MCP_BRIDGE_VERSION,
        mcp_tool_schema_version=MCP_TOOL_SCHEMA_VERSION,
        service_spec_schema=SERVICE_SPEC_VERSION,
        blue_brief_catalog_version=BLUE_BRIEF_CATALOG_VERSION,
        tool_ceiling=5,
        turn_budget=3,
        token_budget=1536,
        publication_enabled=False,
    )
    normalized = config.normalized()
    assert normalized["schema_version"] == ARENA_CONFIG_SCHEMA_VERSION
    assert normalized["runner_image"] == "sandboxer-runner:v1"
    assert normalized["runner_base_image_digest"] == RUNNER_BASE_IMAGE_DIGEST
    assert normalized["mcp_bridge_version"] == "sandboxer.mcp-bridge.v1"
    assert normalized["mcp_tool_schema_version"] == "sandboxer.runner-tools.v1"
    assert normalized["service_spec_schema"] == "sandboxer.service-spec.v1"
    assert normalized["blue_brief_catalog_version"] == "sandboxer.blue-brief.v1"
    assert normalized["tool_ceiling"] == 5
    assert normalized["turn_budget"] == 3
    assert normalized["token_budget"] == 1536
    assert isinstance(config.config_digest, str)
    assert len(config.config_digest) == 64


# ---------------------------------------------------------------------------
# 2. Recording of Configuration Digest & Base Image Digest in Telemetry
# ---------------------------------------------------------------------------


def test_series_and_matches_record_arena_config_and_base_image_digest() -> None:
    spec = _make_spec()
    bundle = execute_series(spec)

    created_events = [e for e in bundle.telemetry if e.get("event_type") == "SERIES_CREATED"]
    assert len(created_events) == 1
    created = created_events[0]
    assert "arena_config_digest" in created
    assert "runner_base_image_digest" in created
    assert created["runner_base_image_digest"] == RUNNER_BASE_IMAGE_DIGEST
    assert len(created["arena_config_digest"]) == 64

    match_started_events = [e for e in bundle.telemetry if e.get("event_type") == "MATCH_STARTED"]
    assert len(match_started_events) >= 1
    for event in match_started_events:
        assert event["arena_config_digest"] == created["arena_config_digest"]
        assert event["runner_base_image_digest"] == RUNNER_BASE_IMAGE_DIGEST
        assert event["service_spec_schema"] == SERVICE_SPEC_VERSION
        assert event["blue_brief_catalog_version"] == BLUE_BRIEF_CATALOG_VERSION
        assert event["mcp_bridge_version"] == MCP_BRIDGE_VERSION
        assert event["tool_ceiling"] == spec.match_policy.tool_budget
        assert event["turn_limit"] == spec.match_policy.turn_budget


# ---------------------------------------------------------------------------
# 3. Seed as Minor Functional Selector Only (No Vulnerability Injection)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seed", ["alpha-01", "beta-02", "gamma-03", "delta-42", "prod-2026"])
def test_seed_isolation_does_not_inject_forbidden_semantics_or_vulnerabilities(seed: str) -> None:
    briefs = select_blue_briefs(seed, count=3)
    assert len(briefs) == 3
    for brief in briefs:
        validation_reasons = validate_blue_brief(brief)
        assert validation_reasons == ()
        combined_text = f"{brief.family} {brief.outcome} {brief.probe_description}".lower()
        for forbidden in ("weakness", "exploit", "attack path", "flag{"):
            assert forbidden not in combined_text
        # Parameter namespace check
        assert brief.parameters["route_namespace"].startswith("/api/")
        assert brief.parameters["public_note"].startswith("welcome-")


def test_seed_permutation_only_affects_selection_order_and_namespaces() -> None:
    briefs_a = select_blue_briefs("seed-123")
    briefs_b = select_blue_briefs("seed-456")
    assert {b.family for b in briefs_a} == {b.family for b in briefs_b}
    for b in briefs_a + briefs_b:
        assert b.parameters["route_namespace"].startswith("/api/")
        assert b.parameters["public_note"].startswith("welcome-")


# ---------------------------------------------------------------------------
# 4. Calibration Matches Unpublishable by Default
# ---------------------------------------------------------------------------


def test_calibration_series_is_unpublishable_by_default() -> None:
    spec = _make_spec(is_calibration=True)
    bundle = execute_series(spec)

    assert bundle.publication_eligible is False
    created_events = [e for e in bundle.telemetry if e.get("event_type") == "SERIES_CREATED"]
    assert created_events[0]["is_calibration"] is True
    assert created_events[0]["publication_enabled"] is False

    match_started = [e for e in bundle.telemetry if e.get("event_type") == "MATCH_STARTED"]
    assert all(e["is_calibration"] is True for e in match_started)

    with pytest.raises(ResultReportError, match="CALIBRATION_SERIES_NOT_PUBLISHABLE"):
        build_result_report(bundle.evidence_bundle)


def test_diagnose_dry_run_marks_calibration_unpublishable() -> None:
    events = [
        {"event_id": "e1", "event": "match_started", "monotonic_seconds": 0},
        {"event_id": "e2", "event": "match_finished", "monotonic_seconds": 10, "terminal_reason": "winner_decided"},
    ]
    diag = diagnose_dry_run(events, blue_briefs=["portable_notes"])
    assert diag["is_calibration"] is True
    assert diag["publication_eligible"] is False


def test_spec_with_publication_disabled_is_unpublishable() -> None:
    spec = _make_spec(publication_enabled=False)
    bundle = execute_series(spec)
    assert bundle.publication_eligible is False

    with pytest.raises(ResultReportError, match="CALIBRATION_SERIES_NOT_PUBLISHABLE"):
        build_result_report(bundle.evidence_bundle)


# ---------------------------------------------------------------------------
# 5. Explicit Outcome Definition (VALID_CAPTURE, VALID_NO_CAPTURE, BUDGET_EXHAUSTED, INVALID)
# ---------------------------------------------------------------------------


def test_match_outcome_resolution_helper() -> None:
    assert resolve_match_outcome("SOLE_CAPTURE") == MatchOutcome.VALID_CAPTURE
    assert resolve_match_outcome("DUAL_CAPTURE_HEALTH") == MatchOutcome.VALID_CAPTURE
    assert resolve_match_outcome("DUAL_CAPTURE_SUBMISSION_ORDER") == MatchOutcome.VALID_CAPTURE

    assert resolve_match_outcome("NO_CAPTURE_AVAILABILITY") == MatchOutcome.VALID_NO_CAPTURE
    assert resolve_match_outcome("EXACT_TIE") == MatchOutcome.VALID_NO_CAPTURE

    assert resolve_match_outcome("COMPETITIVE_BUDGET_EXHAUSTED") == MatchOutcome.BUDGET_EXHAUSTED
    assert resolve_match_outcome("MATCH_TIMEOUT") == MatchOutcome.BUDGET_EXHAUSTED

    assert resolve_match_outcome("SOLE_CAPTURE", teardown="uncertain") == MatchOutcome.INVALID
    assert resolve_match_outcome("SOLE_CAPTURE", quarantined=True) == MatchOutcome.INVALID
    assert resolve_match_outcome("SOLE_CAPTURE", auditor_valid=False) == MatchOutcome.INVALID
    assert resolve_match_outcome("TEARDOWN_UNCERTAIN") == MatchOutcome.INVALID
    assert resolve_match_outcome("DEPLOYMENT_REJECTED") == MatchOutcome.INVALID


def test_series_records_explicit_outcomes_in_match_results_and_telemetry() -> None:
    # 1. Valid Capture
    spec_capture = _make_spec(beta_response="finish")
    bundle_capture = execute_series(spec_capture)
    assert all(r["outcome"] == MatchOutcome.VALID_CAPTURE for r in bundle_capture.match_results)
    finished_events = [e for e in bundle_capture.telemetry if e.get("event_type") == "MATCH_FINISHED"]
    assert all(e["outcome"] == MatchOutcome.VALID_CAPTURE for e in finished_events)

    # 2. Valid No Capture (tie/finish without capture)
    spec_no_capture = SeriesSpec(
        schema_version="sandboxer.series-spec.v1",
        series_id="no-capture-001",
        seed="no-capture-seed",
        competitors=(
            ControlledCompetitor("atlas", FakeModelAdapter("atlas", ("defend", "finish"))),
            ControlledCompetitor("borealis", FakeModelAdapter("borealis", ("defend", "finish"))),
        ),
        match_policy=MatchPolicy(best_of=3, output_token_budget=100, turn_budget=2, tool_budget=2),
        runner_backend=FakeRunnerBackend(),
    )
    bundle_no_capture = execute_series(spec_no_capture)
    assert all(r["outcome"] == MatchOutcome.VALID_NO_CAPTURE for r in bundle_no_capture.match_results)

    # 3. Budget Exhausted
    spec_budget = _make_spec(beta_response="tool tool tool")
    bundle_budget = execute_series(spec_budget)
    assert all(r["outcome"] == MatchOutcome.BUDGET_EXHAUSTED for r in bundle_budget.match_results)

    # 4. Invalid (Teardown Uncertain)
    spec_invalid = _make_spec(runner_teardown="uncertain")
    bundle_invalid = execute_series(spec_invalid)
    assert all(r["outcome"] == MatchOutcome.INVALID for r in bundle_invalid.match_results)


# ---------------------------------------------------------------------------
# 6. Gate Enforcement on Report Generation
# ---------------------------------------------------------------------------


def test_report_gate_accepts_clean_bundle_and_rejects_invalid_matches() -> None:
    # Clean run succeeds
    spec = _make_spec()
    bundle = execute_series(spec)
    report = build_result_report(bundle.evidence_bundle)
    assert report.model["outcome"]["winner"] == "atlas"
    assert len(report.model["technical_chapters"]) == 2
    assert all(
        ch["outcome"]["winner"] in {"atlas", "borealis"}
        for ch in report.model["technical_chapters"]
    )


def test_report_gate_rejects_teardown_failure() -> None:
    bundle_dict = json.loads(json.dumps(execute_series(_make_spec()).evidence_bundle))
    # Alter teardown status in public normalized telemetry
    for event in bundle_dict["public"]["normalized_telemetry"]:
        if event.get("event_type") == "RUNNER_TEARDOWN":
            event["status"] = "quarantined"
    # Recomputing digest to isolate the report gate check
    bundle_dict["checksums"]["public"] = sha256(
        json.dumps(bundle_dict["public"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    with pytest.raises(ResultReportError):
        build_result_report(bundle_dict)
