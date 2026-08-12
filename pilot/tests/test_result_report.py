from __future__ import annotations

import json
from hashlib import sha256
from io import BytesIO

import pytest
from pypdf import PdfReader

from sandboxer_v0 import (
    ControlledCompetitor,
    FakeModelAdapter,
    FakeRunnerBackend,
    MatchPolicy,
    SeriesSpec,
    EvidenceVersionStore,
    execute_series,
)
from sandboxer_v0.report import ResultReportError, build_result_report, render_report_pdf


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
    assert model["incident_appendix"] == ()
    assert all(claim["type"] in {"Observed", "Derived", "Interpreted", "Hypothesis"} for claim in model["claims"])
    assert all(claim["evidence"]["event_ids"] for claim in model["claims"])
    assert all(
        {"material_alternatives", "confidence", "scope", "sample_size"} <= claim.keys()
        for claim in model["claims"]
        if claim["type"] == "Interpreted"
    )
    assert all(comparison["does_not_establish"] == "intent" for chapter in model["technical_chapters"] for comparison in chapter["interview_red_comparisons"])

    assert json.loads(report.json) == model.to_dict()
    assert '<html lang="en">' in report.html
    assert '<main id="result-report">' in report.html
    assert "@media print" in report.html
    assert "aria-describedby" in report.html
    assert "Alpha" not in report.html and "Beta" not in report.html
    assert report.pdf.startswith(b"%PDF-1.7")
    assert b"/Lang (en-US)" in report.pdf
    assert b"experimental benchmark in a simulated CTF Arena" in report.pdf
    assert all(claim["id"].encode() in report.html.encode() and claim["id"].encode() in report.pdf for claim in model["claims"])
    parsed_pdf = PdfReader(BytesIO(report.pdf))
    assert len(parsed_pdf.pages) >= 1
    assert "/StructTreeRoot" in parsed_pdf.trailer["/Root"]
    assert all(tag in report.pdf for tag in (b"/S /H1", b"/S /H2", b"/S /P", b"/S /Table", b"/S /L", b"/S /Link"))
    assert "Winner: atlas" in "".join(page.extract_text() for page in parsed_pdf.pages)


def test_result_report_requires_a_valid_signed_frozen_evidence_bundle() -> None:
    bundle = execute_series(_spec()).evidence_bundle
    invalid = dict(bundle, public=dict(bundle["public"], auditor_verdict=dict(bundle["public"]["auditor_verdict"], valid=False)))

    with pytest.raises(ResultReportError, match="VALID_FROZEN_EVIDENCE_REQUIRED"):
        build_result_report(invalid)


@pytest.mark.parametrize("path", [("public", "score_proof"), ("restricted", "telemetry_checksum"), ("checksums", "public"), ("signature",)])
def test_result_report_rejects_each_forged_frozen_bundle_component(path: tuple[str, ...]) -> None:
    bundle = execute_series(_spec()).evidence_bundle
    forged = json.loads(json.dumps(bundle))
    target = forged
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = "0" * 64 if isinstance(target[path[-1]], str) else []

    with pytest.raises(ResultReportError, match="VALID_FROZEN_EVIDENCE_REQUIRED"):
        build_result_report(forged)


def test_report_model_is_deeply_immutable_and_json_is_its_semantic_projection() -> None:
    report = build_result_report(execute_series(_spec()).evidence_bundle)

    with pytest.raises(TypeError):
        report.model["outcome"]["winner"] = "forged"
    with pytest.raises(AttributeError):
        report.model["technical_chapters"].append("forged")
    assert json.loads(report.json) == report.model.to_dict()
    document = report.model["document"]
    assert document["type"] == "document"
    assert {"heading", "paragraph", "table", "list", "link", "incident", "claim"} <= {block["type"] for block in document["children"]}
    assert all(claim["id"] in {block.get("id") for block in document["children"]} for claim in report.model["claims"])


def test_pdf_preserves_unicode_identity_as_accessible_actual_text_without_question_mark_replacement() -> None:
    report = build_result_report(execute_series(_spec()).evidence_bundle)
    model = report.model.to_dict()
    model["title"] = "Atlás – 模型 Result Report"
    model["document"]["children"][0]["text"] = model["title"]

    pdf = render_report_pdf(model)

    encoded_title = (b"\xfe\xff" + model["title"].encode("utf-16-be")).hex().upper().encode()
    assert encoded_title in pdf
    assert b"Atlas [U+2013] [U+6A21][U+578B] Result Report" in pdf
    assert b"Atlas ? ?? Result Report" not in pdf


def test_report_rejects_a_fully_rehashed_public_winner_forgery_that_keeps_raw_seals() -> None:
    bundle = json.loads(json.dumps(execute_series(_spec()).evidence_bundle))
    events = bundle["public"]["normalized_telemetry"]
    previous = "0" * 64
    for event in events:
        if event["event_type"] in {"MATCH_FINISHED", "SERIES_COMPLETED"}:
            event["winner"] = "borealis"
        event["previous_event_hash"] = previous
        event.pop("event_hash")
        event["event_hash"] = sha256(json.dumps(event, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        previous = event["event_hash"]
    bundle["public"]["score_proof"] = [
        {**item, "winner": "borealis"} for item in bundle["public"]["score_proof"]
    ]
    bundle["checksums"]["public"] = sha256(json.dumps(bundle["public"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    unsigned = {key: bundle[key] for key in ("version", "url", "previous_version_url", "previous_bundle_hash", "provenance", "checksums")}
    bundle["bundle_hash"] = sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    bundle["signature"] = sha256(json.dumps({"bundle_hash": bundle["bundle_hash"], "verdict_signature": bundle["public"]["auditor_verdict"]["signature"]}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    with pytest.raises(ResultReportError, match="VALID_FROZEN_EVIDENCE_REQUIRED"):
        build_result_report(bundle)


def test_corrected_evidence_keeps_prior_report_addressable_in_a_visible_chain() -> None:
    spec = _spec()
    outcome = execute_series(spec)
    store = EvidenceVersionStore()
    first = store.freeze(spec=spec, telemetry=outcome.telemetry, results=outcome.match_results, verdict=outcome.verdict)
    corrected = store.freeze(spec=spec, telemetry=outcome.telemetry, results=outcome.match_results, verdict=outcome.verdict)

    report = build_result_report(corrected, correction_index=store.correction_index())
    superseded = build_result_report(first, correction_index=store.correction_index())

    assert report.model["corrections"]["current_report_url"] == "sandboxer://reports/result-report-fixture/v2"
    assert report.model["corrections"]["supersedes_report_url"] == "sandboxer://reports/result-report-fixture/v1"
    assert first.bundle_hash == report.model["corrections"]["previous_evidence_hash"]
    assert report.model["corrections"]["status"] == "current"
    assert superseded.model["corrections"]["status"] == "superseded"
    assert superseded.model["corrections"]["superseded_by_report_url"] == "sandboxer://reports/result-report-fixture/v2"
    assert "Earlier versions remain addressable" in report.html
