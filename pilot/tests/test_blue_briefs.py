from sandboxer_v0.blue_briefs import (
    BLUE_BRIEF_CATALOG_VERSION,
    BLUE_BRIEFS,
    select_blue_briefs,
    validate_blue_brief,
)
from sandboxer_v0 import ControlledCompetitor, FakeModelAdapter, FakeRunnerBackend, MatchPolicy, SeriesSpec, execute_series


def test_catalog_is_versioned_outcome_focused_and_safe() -> None:
    assert BLUE_BRIEF_CATALOG_VERSION == "sandboxer.blue-brief.v1"
    assert 3 <= len(BLUE_BRIEFS) <= 5
    for brief in BLUE_BRIEFS:
        assert brief.version == BLUE_BRIEF_CATALOG_VERSION
        assert brief.outcome and brief.family
        assert "weakness" not in brief.outcome.lower()
        assert "exploit" not in brief.outcome.lower()
        assert validate_blue_brief(brief) == ()


def test_seed_selects_same_symmetric_parameters_without_replacement() -> None:
    first = select_blue_briefs("episode-1", count=3)
    second = select_blue_briefs(1, count=3)
    assert first == select_blue_briefs("episode-1", count=3)
    assert second == select_blue_briefs(1, count=3)
    assert len({brief.family for brief in first}) == 3
    assert all(brief.parameters["public_note"] == first[0].parameters["public_note"] for brief in first)
    assert all(brief.parameters["route_namespace"] == first[0].parameters["route_namespace"] for brief in first)
    assert all("flag" not in key.lower() for brief in first for key in brief.parameters)


def test_selection_rejects_catalog_overflow() -> None:
    import pytest

    with pytest.raises(ValueError, match="without replacement"):
        select_blue_briefs("episode-1", count=len(BLUE_BRIEFS) + 1)


def test_common_contract_probes_are_shared_and_require_safe_service_evidence() -> None:
    brief = select_blue_briefs("episode-1", count=1)[0]
    assert brief.probes == (
        "health",
        "functional_integrity",
        "safety",
    )
    assert validate_blue_brief(brief, probe_results={"health": True, "functional_integrity": True, "safety": True}) == ()
    assert "functional_integrity" in validate_blue_brief(brief, probe_results={"health": True, "functional_integrity": False, "safety": True})


def test_release_bundle_carries_brief_proof_and_non_claiming_diversity_signals() -> None:
    spec = SeriesSpec(
        schema_version="sandboxer.series-spec.v1",
        series_id="brief-proof",
        seed="brief-seed",
        competitors=(
            ControlledCompetitor("atlas", FakeModelAdapter("atlas", ("defend", "capture"))),
            ControlledCompetitor("borealis", FakeModelAdapter("borealis", ("defend", "finish"))),
        ),
        match_policy=MatchPolicy(best_of=3, output_token_budget=100, turn_budget=2, tool_budget=2),
        runner_backend=FakeRunnerBackend(),
    )
    bundle = execute_series(spec)
    assert all(result["blue_brief_version"] == BLUE_BRIEF_CATALOG_VERSION for result in bundle.match_results)
    assert all(result["blue_brief_parameters"] == bundle.match_results[0]["blue_brief_parameters"] for result in bundle.match_results)
    probes = [event for event in bundle.telemetry if event["event_type"] == "RUNNER_PROBE_RESULT"]
    assert len(probes) == bundle.matches_completed * 2
    assert all(event["probes"] == {"health": True, "functional_integrity": True, "safety": True} for event in probes)
    signals = bundle.calibration["blue_brief_diversity"]["signals"]
    assert signals["weakness_family"] == "not-evaluable"
    assert signals["attack_path_repetition"] == "not-evaluable"
