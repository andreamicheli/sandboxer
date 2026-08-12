from __future__ import annotations

import json

import pytest

from sandboxer_v0 import (
    ControlledCompetitor,
    FakeModelAdapter,
    FakeRunnerBackend,
    MatchPolicy,
    SeriesSpec,
    EvidenceVersionStore,
    execute_series,
)
from sandboxer_v0.report import ResultReportError, build_result_report


def _spec() -> SeriesSpec:
    return SeriesSpec(
        schema_version="sandboxer.series-spec.v1",
        series_id="result-report-fixture",
        seed="result-report-seed",
        competitors=(
            ControlledCompetitor("atlas", FakeModelAdapter("fixture/atlas", ("defend", "capture"))),
            ControlledCompetitor("borealis", FakeModelAdapter("fixture/borealis", ("defend", "finish"))),
        ),
        match_policy=MatchPolicy(best_of=3, output_token_budget=100, turn_budget=2, tool_budget=2),
        runner_backend=FakeRunnerBackend(),
    )


def test_result_report_projects_one_valid_frozen_bundle_into_synchronized_public_formats() -> None:
    bundle = execute_series(_spec()).evidence_bundle

    report = build_result_report(bundle)

    model = report.model
    assert model["schema_version"] == "sandboxer.result-report.v1"
    assert model["language"] == "en"
    assert model["source_evidence"] == {
        "url": "sandboxer://evidence/result-report-fixture/v1",
        "version": 1,
        "bundle_hash": bundle["bundle_hash"],
    }
    assert model["outcome"]["winner"] == "atlas"
    assert model["scope"]["label"] == "experimental benchmark in a simulated CTF Arena"
    assert len(model["technical_chapters"]) == 2
    assert model["incident_appendix"] == []
    assert all(claim["type"] in {"Observed", "Derived", "Interpreted", "Hypothesis"} for claim in model["claims"])
    assert all(claim["evidence"]["event_ids"] for claim in model["claims"])
    assert all(
        {"material_alternatives", "confidence", "scope", "sample_size"} <= claim.keys()
        for claim in model["claims"]
        if claim["type"] == "Interpreted"
    )
    assert all(comparison["does_not_establish"] == "intent" for chapter in model["technical_chapters"] for comparison in chapter["interview_red_comparisons"])

    assert json.loads(report.json) == model
    assert '<html lang="en">' in report.html
    assert '<main id="result-report">' in report.html
    assert "@media print" in report.html
    assert "aria-describedby" in report.html
    assert "Alpha" not in report.html and "Beta" not in report.html
    assert report.pdf.startswith(b"%PDF-1.7")
    assert b"/Lang (en-US)" in report.pdf
    assert b"experimental benchmark in a simulated CTF Arena" in report.pdf
    assert all(claim["id"].encode() in report.html.encode() and claim["id"].encode() in report.pdf for claim in model["claims"])


def test_result_report_requires_a_valid_signed_frozen_evidence_bundle() -> None:
    bundle = execute_series(_spec()).evidence_bundle
    invalid = dict(bundle, public=dict(bundle["public"], auditor_verdict=dict(bundle["public"]["auditor_verdict"], valid=False)))

    with pytest.raises(ResultReportError, match="VALID_FROZEN_EVIDENCE_REQUIRED"):
        build_result_report(invalid)


def test_corrected_evidence_keeps_prior_report_addressable_in_a_visible_chain() -> None:
    spec = _spec()
    outcome = execute_series(spec)
    store = EvidenceVersionStore()
    first = store.freeze(spec=spec, telemetry=outcome.telemetry, results=outcome.match_results, verdict=outcome.verdict)
    corrected = store.freeze(spec=spec, telemetry=outcome.telemetry, results=outcome.match_results, verdict=outcome.verdict)

    report = build_result_report(corrected)

    assert report.model["corrections"]["current_report_url"] == "sandboxer://reports/result-report-fixture/v2"
    assert report.model["corrections"]["supersedes_report_url"] == "sandboxer://reports/result-report-fixture/v1"
    assert first.bundle_hash == report.model["corrections"]["previous_evidence_hash"]
    assert "Earlier versions remain addressable" in report.html
