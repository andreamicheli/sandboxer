from __future__ import annotations

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
from sandboxer_v0.report_latex import (
    build_report_latex,
    compute_analytics,
    render_charts,
    render_report_latex,
)


def _bundle() -> dict:
    spec = SeriesSpec(
        schema_version="sandboxer.series-spec.v1",
        series_id="latex-report-fixture",
        seed="latex-report-seed",
        competitors=(
            ControlledCompetitor("atlas", FakeModelAdapter("fixture/atlas", ("defend", "capture"))),
            ControlledCompetitor("borealis", FakeModelAdapter("fixture/borealis", ("defend", "finish"))),
        ),
        match_policy=MatchPolicy(best_of=3, output_token_budget=100, turn_budget=2, tool_budget=2),
        runner_backend=FakeRunnerBackend(),
    )
    return execute_series(spec).evidence_bundle


def _model() -> dict:
    return build_result_report(_bundle()).model.to_dict()


def test_analytics_exposes_per_competitor_measurements() -> None:
    analytics = compute_analytics(_model())
    assert analytics["competitors"] == ["atlas", "borealis"]
    assert len(analytics["matches"]) == 2
    for match in analytics["matches"]:
        for competitor in ("atlas", "borealis"):
            assert set(match["per_competitor"][competitor]) == {"output_tokens", "turns", "tool_calls"}
            assert all(isinstance(value, int) for value in match["per_competitor"][competitor].values())


def test_charts_are_png_bytes() -> None:
    charts = render_charts(compute_analytics(_model()))
    assert set(charts) == {"output_tokens.png", "tool_calls.png", "turns.png", "phase_timeline.png"}
    assert all(content.startswith(b"\x89PNG") for content in charts.values())


def test_latex_renderer_embeds_charts_and_escapes_identities() -> None:
    model = _model()
    analytics = compute_analytics(model)
    tex = render_report_latex(model, analytics, ["output_tokens.png", "phase_timeline.png"])
    assert r"\documentclass" in tex
    assert r"\includegraphics[width=\textwidth]{output_tokens.png}" in tex
    assert r"\section{Match chapters}" in tex
    assert "atlas" in tex and "borealis" in tex
    assert r"\toprule" in tex  # booktabs accounting table


def test_build_report_latex_without_compile_is_deterministic_and_chart_only() -> None:
    report = build_report_latex(_bundle(), compile_pdf=False)
    assert not report.compiled
    assert report.pdf == b""
    assert report.tex.startswith(r"\documentclass")
    assert set(report.charts) == {"output_tokens.png", "tool_calls.png", "turns.png", "phase_timeline.png"}


@pytest.mark.skipif(shutil.which("pdflatex") is None, reason="pdflatex not available")
def test_build_report_latex_compiles_to_pdf() -> None:
    report = build_report_latex(_bundle(), compile_pdf=True)
    assert report.compiled
    assert report.pdf.startswith(b"%PDF")
