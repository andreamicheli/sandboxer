from __future__ import annotations

import json
import shutil

import pytest

from sandboxer_v0 import (
    ControlledCompetitor,
    FakeModelAdapter,
    FakeRunnerBackend,
    MatchPolicy,
    SeriesSpec,
    execute_series,
)
from sandboxer_v0.report import build_result_report
from sandboxer_v0.report_latex import compile_latex, compute_analytics, render_charts, render_narrative_latex
from sandboxer_v0.report_narrative import (
    DeterministicReportNarrativeDrafter,
    GeminiReportNarrativeDrafter,
    ReportNarrativeError,
    draft_report_narrative,
    render_narrative_html,
    validate_report_narrative,
)


def _model() -> dict:
    spec = SeriesSpec(
        schema_version="sandboxer.series-spec.v1",
        series_id="narrative-fixture",
        seed="narrative-seed",
        competitors=(
            ControlledCompetitor("atlas", FakeModelAdapter("fixture/atlas", ("defend", "capture"))),
            ControlledCompetitor("borealis", FakeModelAdapter("fixture/borealis", ("defend", "finish"))),
        ),
        match_policy=MatchPolicy(best_of=3, output_token_budget=100, turn_budget=2, tool_budget=2),
        runner_backend=FakeRunnerBackend(),
    )
    return build_result_report(execute_series(spec).evidence_bundle).model.to_dict()


def _narrative() -> dict:
    return DeterministicReportNarrativeDrafter().draft(_model())


def test_deterministic_drafter_is_valid() -> None:
    model = _model()
    narrative = _narrative()
    assert validate_report_narrative(narrative, model) == ()
    assert set(narrative) == {"summary", "per_match", "analysis", "limitations"}
    numbers = [item["match_number"] for item in narrative["per_match"]]
    assert numbers == sorted(chapter["match_number"] for chapter in model["technical_chapters"])


def test_validate_rejects_empty_and_bad_match_number() -> None:
    model = _model()
    narrative = _narrative()
    narrative["summary"] = ""
    assert any("summary" in failure for failure in validate_report_narrative(narrative, model))
    narrative = _narrative()
    narrative["per_match"][0]["match_number"] = 999
    assert any("bad match_number" in failure for failure in validate_report_narrative(narrative, model))


def test_validate_rejects_duplicate_match_number() -> None:
    model = _model()
    narrative = _narrative()
    narrative["per_match"][1]["match_number"] = narrative["per_match"][0]["match_number"]
    assert any("duplicate" in failure for failure in validate_report_narrative(narrative, model))


def test_validate_rejects_credential_leak() -> None:
    model = _model()
    narrative = _narrative()
    narrative["analysis"] = "leaked key sk-abcdef1234567890 in the text"
    assert any("credential leak" in failure for failure in validate_report_narrative(narrative, model))


class _BadDrafter:
    def draft(self, model: dict) -> dict:
        return {"summary": "", "per_match": [], "analysis": "", "limitations": ""}


def test_draft_report_narrative_raises_on_invalid() -> None:
    with pytest.raises(ReportNarrativeError):
        draft_report_narrative(_model(), drafter=_BadDrafter())


class _FakeClient:
    def __init__(self, text: str):
        self._text = text
        self.models = self

    def generate_content(self, *, model: str, contents: str):
        return _FakeResponse(self._text)


class _FakeResponse:
    def __init__(self, text: str):
        self.text = text


def test_gemini_parse_handles_fence_and_float_match_number() -> None:
    model = _model()
    numbers = [chapter["match_number"] for chapter in model["technical_chapters"]]
    payload = {
        "summary": "A short summary.",
        "per_match": [
            {"match_number": float(numbers[0]), "narrative": "Match narrative one."},
            {"match_number": numbers[1], "narrative": "Match narrative two."},
        ],
        "analysis": "Analysis.",
        "limitations": "Limitations.",
    }
    drafter = GeminiReportNarrativeDrafter(client=_FakeClient("```json\n" + json.dumps(payload) + "\n```"))
    narrative = drafter.draft(model)
    assert isinstance(narrative["per_match"][0]["match_number"], int)
    assert narrative["per_match"][0]["match_number"] == numbers[0]


def test_gemini_parse_rejects_bad_json() -> None:
    drafter = GeminiReportNarrativeDrafter(client=_FakeClient("sorry, no JSON here"))
    with pytest.raises(ReportNarrativeError):
        drafter.draft(_model())


def test_render_narrative_html_contains_deterministic_winner_and_links() -> None:
    model = _model()
    html = render_narrative_html(model, _narrative())
    assert model["outcome"]["winner"] in html
    assert "report.html" in html
    assert "narrative.pdf" in html
    assert "Summary" in html


def test_render_narrative_latex_embeds_narrative_and_accounting() -> None:
    model = _model()
    tex = render_narrative_latex(model, _narrative(), ["output_tokens.png"])
    assert r"\section{Summary}" in tex
    assert r"\section{Match narratives}" in tex
    assert r"\subsection{Match" in tex
    assert r"\toprule" in tex  # deterministic accounting table
    assert model["outcome"]["winner"] in tex
    assert "won" in tex  # narrative prose escaped into the document


@pytest.mark.skipif(shutil.which("pdflatex") is None, reason="pdflatex not available")
def test_narrative_latex_compiles_to_pdf() -> None:
    model = _model()
    charts = render_charts(compute_analytics(model))
    tex = render_narrative_latex(model, _narrative(), list(charts.keys()))
    pdf = compile_latex(tex, charts)
    assert pdf.startswith(b"%PDF")
