from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import pytest

from sandboxer_v0 import (
    ControlledCompetitor,
    FakeModelAdapter,
    FakeRunnerBackend,
    MatchPolicy,
    SeriesSpec,
    execute_series,
)
from scripts.publish_broadcast import _build_site_report, _series_slug, _slugify


def _bundle() -> dict:
    spec = SeriesSpec(
        schema_version="sandboxer.series-spec.v1",
        series_id="publish-broadcast-fixture",
        seed="publish-broadcast-seed",
        competitors=(
            ControlledCompetitor("atlas", FakeModelAdapter("fixture/atlas", ("defend", "capture"))),
            ControlledCompetitor("borealis", FakeModelAdapter("fixture/borealis", ("defend", "finish"))),
        ),
        match_policy=MatchPolicy(best_of=3, output_token_budget=100, turn_budget=2, tool_budget=2),
        runner_backend=FakeRunnerBackend(),
    )
    return execute_series(spec).evidence_bundle


def test_slugify_normalizes_to_safe_slug() -> None:
    assert _slugify("Best-of-3 / Match! 001") == "best-of-3-match-001"
    assert _slugify("   ") == ""


def test_series_slug_derives_from_series_id_and_falls_back() -> None:
    assert _series_slug(_bundle()) == "publish-broadcast-fixture"
    stripped = dict(_bundle())
    stripped["public"] = {**stripped["public"], "specification": {}}
    assert _series_slug(stripped).startswith("series-")


def _ns(tmp_path: Path, **overrides) -> argparse.Namespace:
    bundle_file = tmp_path / "bundle.json"
    bundle_file.write_text(json.dumps(_bundle()), encoding="utf-8")
    values: dict = {
        "bundle": bundle_file,
        "report_slug": None,
        "site_base_url": "https://sandboxer.example",
        "site_root": tmp_path / "site",
        "report_url": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_build_site_report_no_bundle_is_legacy_noop(tmp_path: Path) -> None:
    result = _build_site_report(_ns(tmp_path, bundle=None), {}, "2026-08-17")
    assert result == {"report_url": None, "slug": None, "publication": None, "broadcast_report": None}


@pytest.mark.skipif(shutil.which("pdflatex") is None, reason="pdflatex not available")
def test_build_site_report_stages_files_and_entry(tmp_path: Path) -> None:
    ns = _ns(tmp_path)
    result = _build_site_report(ns, {}, "2026-08-17")
    assert result["slug"] == "publish-broadcast-fixture"
    assert result["report_url"] == "https://sandboxer.example/assets/reports/publish-broadcast-fixture/report.html"
    reports_dir = ns.site_root / "assets" / "reports" / "publish-broadcast-fixture"
    for name in ("report.html", "report.json", "report.pdf", "report-detailed.tex", "report-detailed.pdf", "evidence.json"):
        assert (reports_dir / name).is_file(), name
    assert (reports_dir / "charts" / "output_tokens.png").is_file()
    publication = result["publication"]
    assert publication["winner"] in {"atlas", "borealis"}
    assert publication["video_url"] is None
    assert publication["publish_at"] == "2026-08-17"
    assert publication["report_url"] == result["report_url"]
    assert result["broadcast_report"]["title"].startswith("Sandboxer Series:")
    assert publication["models"] and all("(" in item for item in publication["models"])
