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
from sandboxer_v0.report_narrative import DeterministicReportNarrativeDrafter, draft_report_narrative
from sandboxer_v0.video import build_video_manifest
from scripts import publish_broadcast as pb
from scripts.publish_broadcast import _build_site_report, _series_slug, _slugify

INTRO_ASSET = Path(pb.__file__).resolve().parents[2] / "video" / "public" / "assets" / "custom-intro.mp4"


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


def _deterministic_narrative(model):
    return draft_report_narrative(model, drafter=DeterministicReportNarrativeDrafter())


def test_build_site_report_no_bundle_is_legacy_noop(tmp_path: Path) -> None:
    result = _build_site_report(_ns(tmp_path, bundle=None), {}, "2026-08-17")
    assert result == {"report_url": None, "slug": None, "publication": None, "broadcast_report": None}


@pytest.mark.skipif(shutil.which("pdflatex") is None, reason="pdflatex not available")
def test_build_site_report_stages_files_and_entry(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(pb, "_draft_narrative", _deterministic_narrative)
    ns = _ns(tmp_path)
    result = _build_site_report(ns, {}, "2026-08-17")
    assert result["slug"] == "publish-broadcast-fixture"
    assert result["report_url"] == "https://sandboxer.example/assets/reports/publish-broadcast-fixture/narrative.html"
    reports_dir = ns.site_root / "assets" / "reports" / "publish-broadcast-fixture"
    for name in (
        "report.html",
        "report.json",
        "report.pdf",
        "report-detailed.tex",
        "report-detailed.pdf",
        "narrative.html",
        "narrative.tex",
        "narrative.pdf",
        "evidence.json",
    ):
        assert (reports_dir / name).is_file(), name
    assert (reports_dir / "charts" / "output_tokens.png").is_file()
    publication = result["publication"]
    assert publication["winner"] in {"atlas", "borealis"}
    assert publication["video_url"] is None
    assert publication["publish_at"] == "2026-08-17"
    assert publication["report_url"] == result["report_url"]
    assert result["broadcast_report"]["title"].startswith("Sandboxer Series:")
    assert publication["models"] and all("(" in item for item in publication["models"])


# --- Cover/thumbnail: first frame of the custom intro asset.


def _replay() -> dict:
    return {
        "schema_version": "sandboxer.replay.v1",
        "source_bundle_hash": "a" * 64,
        "panes": [{"identity": "DeepSeek V4 Pro"}, {"identity": "MiMo V2.5 Pro"}],
        "layout": {"split": {"left": .5, "right": .5, "permanent": True}},
        "frames": [
            {"sequence": 1, "at_monotonic_ns": 0, "event_id": "e1", "event_type": "MATCH_STARTED", "phase": "blue", "pane": 0, "text": "defend"},
            {"sequence": 2, "at_monotonic_ns": 2_000_000_000, "event_id": "e2", "event_type": "MODEL_RESPONSE", "phase": "blue", "pane": 1, "text": "inspect"},
            {"sequence": 3, "at_monotonic_ns": 4_000_000_000, "event_id": "e3", "event_type": "MATCH_FINISHED", "phase": "finalizing", "pane": 0, "text": ""},
        ],
    }


def _run_main(tmp_path: Path, monkeypatch, extra_args=()):
    manifest_file = tmp_path / "manifest.json"
    manifest_file.write_text(json.dumps(build_video_manifest(_replay(), report={}, model_metadata={}, benchmark_snapshot={})), encoding="utf-8")
    video = tmp_path / "delivery.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42" * 64)
    out = tmp_path / "artifacts" / "broadcast.json"
    calls = []
    real_set = pb.YoutubeUploader.set_thumbnail

    def recording_set(self, video_id, thumbnail_path):
        calls.append(Path(thumbnail_path))
        return real_set(self, video_id, thumbnail_path)

    monkeypatch.setattr(pb.YoutubeUploader, "set_thumbnail", recording_set)
    exit_code = pb.main([
        "--manifest", str(manifest_file),
        "--video", str(video),
        "--tts", "fake",
        "--youtube", "fake",
        "--no-bgm",
        "--out", str(out),
        *extra_args,
    ])
    record = json.loads(out.read_text(encoding="utf-8"))
    return exit_code, calls, out.parent, record


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not available")
@pytest.mark.skipif(not INTRO_ASSET.is_file(), reason="custom intro asset missing")
def test_extract_intro_cover_creates_the_jpg_from_the_real_asset(tmp_path: Path) -> None:
    cover = tmp_path / "nested" / "thumbnail.jpg"
    result = pb.extract_intro_cover(INTRO_ASSET, cover)
    assert result == cover
    assert cover.is_file() and cover.stat().st_size > 0
    assert cover.read_bytes()[:3] == b"\xff\xd8\xff"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not available")
@pytest.mark.skipif(not INTRO_ASSET.is_file(), reason="custom intro asset missing")
def test_auto_cover_extracted_from_intro_and_passed_to_set_thumbnail(tmp_path, monkeypatch) -> None:
    exit_code, calls, artifacts_dir, record = _run_main(tmp_path, monkeypatch)
    auto_thumb = artifacts_dir / "thumbnail.jpg"
    assert exit_code == 0
    assert auto_thumb.is_file() and auto_thumb.read_bytes()[:3] == b"\xff\xd8\xff"
    assert calls == [auto_thumb]
    assert record["thumbnail"]["video_id"] == record["youtube"]["video_id"]
    assert record["youtube"]["video_id"].startswith("sbx-video-")


def test_explicit_thumb_overrides_intro_auto_extraction(tmp_path, monkeypatch) -> None:
    explicit = tmp_path / "cover.png"
    explicit.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    exit_code, calls, artifacts_dir, record = _run_main(tmp_path, monkeypatch, ["--thumb", str(explicit)])
    assert exit_code == 0
    assert calls == [explicit]
    assert not (artifacts_dir / "thumbnail.jpg").exists()
    assert record["thumbnail"]["video_id"] == record["youtube"]["video_id"]


def test_no_thumb_and_no_intro_uploads_without_a_thumbnail_call(tmp_path, monkeypatch) -> None:
    exit_code, calls, artifacts_dir, record = _run_main(tmp_path, monkeypatch, ["--intro-asset", str(tmp_path / "missing-intro.mp4")])
    assert exit_code == 0
    assert calls == []
    assert record["thumbnail"] is None
    assert not (artifacts_dir / "thumbnail.jpg").exists()
    assert record["youtube"]["video_id"].startswith("sbx-video-")
