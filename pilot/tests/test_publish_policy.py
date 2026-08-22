"""Publication policy: automatic unlisted uploads, approval-gated public flips.

Covers the four policy rules: (1) uploads default to ``unlisted``;
(2) ``privacy_status="public"`` is refused at upload time; (3) flipping an
uploaded video to public requires a non-empty approval token; (4) the
broadcast script indexes the publication BEFORE uploading so the hard publish
gate has an indexed entry to check, and fills ``video_url`` afterwards.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest import mock

import pytest

from sandboxer_v0 import (
    ControlledCompetitor,
    FakeModelAdapter,
    FakeRunnerBackend,
    MatchPolicy,
    SeriesSpec,
    execute_series,
)
from sandboxer_v0.publication_index import load_index
from sandboxer_v0.publish_gate import PublishGateError, check_publish_gate
from sandboxer_v0.report_narrative import DeterministicReportNarrativeDrafter, draft_report_narrative
from sandboxer_v0.video import build_video_manifest
from sandboxer_v0.youtube import (
    APPROVAL_TOKEN_ENV,
    ApprovalRequiredError,
    DEFAULT_PRIVACY_STATUS,
    FakeYoutubeService,
    YoutubeError,
    YoutubeUploader,
)
from scripts import publish_broadcast as pb


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


def _video(tmp_path: Path) -> Path:
    video = tmp_path / "delivery.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42" * 64)
    return video


# --- Rule 1: uploads default to unlisted.


def test_default_privacy_constant_is_unlisted():
    assert DEFAULT_PRIVACY_STATUS == "unlisted"


def test_upload_defaults_to_unlisted_and_sends_it_in_the_insert_body(tmp_path):
    service = FakeYoutubeService()
    uploader = YoutubeUploader(service=service, dry_run=False)
    result = uploader.upload(_video(tmp_path), title="Sandboxer match", description="Simulated CTF episode", approved=True)
    assert result["privacy_status"] == "unlisted"
    body = service.calls[0]["args"]["body"]
    assert body["status"]["privacyStatus"] == "unlisted"


# --- Rule 2: public visibility is never granted at upload time.


def test_public_at_upload_is_refused_before_anything_reaches_the_api(tmp_path):
    service = FakeYoutubeService()
    uploader = YoutubeUploader(service=service, dry_run=False)
    with pytest.raises(YoutubeError, match="YOUTUBE_PRIVACY_PUBLIC_FORBIDDEN"):
        uploader.upload(
            _video(tmp_path),
            title="Sandboxer match",
            description="Simulated CTF episode",
            privacy_status="public",
            approved=True,
        )
    assert service.calls == []


def test_broadcast_script_refuses_the_public_privacy_choice():
    argv = ["--manifest", "m.json", "--video", "v.mp4", "--privacy", "public"]
    with pytest.raises(SystemExit):
        pb.main(argv)


# --- Rule 3: publish_public requires an explicit, non-empty approval token.


def test_publish_public_without_any_token_raises_approval_required(monkeypatch):
    monkeypatch.delenv(APPROVAL_TOKEN_ENV, raising=False)
    uploader = YoutubeUploader(service=FakeYoutubeService(), dry_run=False)
    with pytest.raises(ApprovalRequiredError, match="PUBLISH_APPROVAL_REQUIRED"):
        uploader.publish_public("sbx-video-0001")


@pytest.mark.parametrize("token", ["", "   "])
def test_publish_public_with_blank_explicit_token_still_raises(monkeypatch, tmp_path, token):
    monkeypatch.delenv(APPROVAL_TOKEN_ENV, raising=False)
    uploader = YoutubeUploader(dry_run=True)
    with pytest.raises(ApprovalRequiredError):
        uploader.publish_public("sbx-video-0001", token)


# --- Rule 3b: with a token, publish_public updates the video status via the API.


def test_publish_public_with_env_token_calls_videos_update_to_public(monkeypatch):
    monkeypatch.setenv(APPROVAL_TOKEN_ENV, "reviewer-decision-123")
    service = FakeYoutubeService()
    uploader = YoutubeUploader(service=service, dry_run=False)
    result = uploader.publish_public("sbx-video-0001")
    assert result["privacy_status"] == "public"
    assert result["url"] == "https://www.youtube.com/watch?v=sbx-video-0001"
    update = service.calls[-1]
    assert update["method"] == "videos.update"
    assert update["args"]["body"]["status"]["privacyStatus"] == "public"
    # The token authorizes the flip but is never recorded anywhere.
    assert "reviewer-decision-123" not in json.dumps(service.calls)
    assert "reviewer-decision-123" not in json.dumps(uploader.calls)


def test_publish_public_accepts_an_explicit_token_without_env(monkeypatch):
    monkeypatch.delenv(APPROVAL_TOKEN_ENV, raising=False)
    service = FakeYoutubeService()
    uploader = YoutubeUploader(service=service, dry_run=False)
    result = uploader.publish_public("sbx-video-0002", "cli-flag-token")
    assert result["privacy_status"] == "public"
    assert service.calls[-1]["method"] == "videos.update"


# --- Rule 4: index before upload so the publish gate has an entry to check.


def _bundle() -> dict:
    spec = SeriesSpec(
        schema_version="sandboxer.series-spec.v1",
        series_id="publish-policy-fixture",
        seed="publish-policy-seed",
        competitors=(
            ControlledCompetitor("atlas", FakeModelAdapter("fixture/atlas", ("defend", "capture"))),
            ControlledCompetitor("borealis", FakeModelAdapter("fixture/borealis", ("defend", "finish"))),
        ),
        match_policy=MatchPolicy(best_of=3, output_token_budget=100, turn_budget=2, tool_budget=2),
        runner_backend=FakeRunnerBackend(),
    )
    return execute_series(spec).evidence_bundle


class _StubLatex:
    tex = "\\documentclass{article}"
    pdf = b"%PDF-stub"
    charts = {}


def _deterministic_narrative(model):
    return draft_report_narrative(model, drafter=DeterministicReportNarrativeDrafter())


UPLOADED = {
    "video_id": "sbx-video-0001",
    "url": "https://www.youtube.com/watch?v=sbx-video-0001",
    "privacy_status": "unlisted",
    "dry_run": False,
}


def test_publish_broadcast_indexes_before_upload_and_fills_video_url_after(tmp_path, monkeypatch):
    bundle_file = tmp_path / "bundle.json"
    bundle_file.write_text(json.dumps(_bundle()), encoding="utf-8")
    manifest_file = tmp_path / "manifest.json"
    manifest_file.write_text(json.dumps(build_video_manifest(_replay(), report={}, model_metadata={}, benchmark_snapshot={})), encoding="utf-8")
    video = _video(tmp_path)
    site_root = tmp_path / "site"
    out = tmp_path / "broadcast.json"

    monkeypatch.setattr(pb, "build_report_latex", lambda bundle, compile_pdf=True: _StubLatex())
    monkeypatch.setattr(pb, "compile_latex", lambda tex, charts: b"%PDF-stub")
    monkeypatch.setattr(pb, "_draft_narrative", _deterministic_narrative)

    events: list[tuple] = []
    real_upsert = pb.upsert_publication

    def recording_upsert(path, entry):
        events.append(("upsert", dict(entry)))
        return real_upsert(path, entry)

    monkeypatch.setattr(pb, "upsert_publication", recording_upsert)

    uploader = mock.MagicMock(name="uploader")
    uploader.preflight.return_value = {"dry_run": False, "channel": {"id": "sbx-channel", "title": "Sandboxer"}}

    def recording_upload(video_path, **kwargs):
        events.append(("upload", kwargs))
        return dict(UPLOADED)

    uploader.upload.side_effect = recording_upload
    # The default intro-cover extraction is stubbed so this ordering test
    # stays hermetic; the extraction itself is covered in its own tests.
    auto_cover = tmp_path / "thumbnail.jpg"

    def stub_extract(intro_path, output_path):
        output_path.write_bytes(b"\xff\xd8\xffstub")
        return output_path

    monkeypatch.setattr(pb, "extract_intro_cover", stub_extract)
    uploader.set_thumbnail.return_value = {
        "video_id": UPLOADED["video_id"],
        "url": f"https://i.ytimg.com/vi/{UPLOADED['video_id']}/hqdefault.jpg",
        "dry_run": False,
    }
    monkeypatch.setattr(pb, "YoutubeUploader", mock.MagicMock(return_value=uploader))

    exit_code = pb.main([
        "--manifest", str(manifest_file),
        "--video", str(video),
        "--tts", "fake",
        "--youtube", "real",
        "--no-bgm",
        "--out", str(out),
        "--bundle", str(bundle_file),
        "--site-root", str(site_root),
        "--site-base-url", "https://sandboxer.example",
        "--yes",
    ])
    assert exit_code == 0

    # Ordering: the slug is indexed BEFORE the upload, video_url filled AFTER.
    assert [kind for kind, _ in events] == ["upsert", "upload", "upsert"]
    pre_upload_entry = events[0][1]
    assert pre_upload_entry["id"] == "publish-policy-fixture"
    assert not pre_upload_entry.get("video_url")  # pending: omitted until upload completes

    # The upload itself carries the gate evidence, including the indexed file.
    upload_kwargs = events[1][1]
    assert upload_kwargs["privacy_status"] == "unlisted"
    assert upload_kwargs["bundle_path"] == bundle_file
    assert upload_kwargs["report_url"] == "https://sandboxer.example/assets/reports/publish-policy-fixture/narrative.html"
    assert upload_kwargs["publications_index"] == site_root / "data" / "publications.json"

    post_upload_entry = events[2][1]
    assert post_upload_entry["video_url"] == UPLOADED["url"]

    index = load_index(site_root / "data" / "publications.json")
    assert index["publications"][0]["video_url"] == UPLOADED["url"]

    record = json.loads(out.read_text(encoding="utf-8"))
    assert record["youtube"]["privacy_status"] == "unlisted"
    assert record["youtube"]["publish_public"] is None
    assert record["publication"]["indexed"] is True
    assert record["publication"]["slug"] == "publish-policy-fixture"

    # The auto-extracted intro cover is set on the uploaded video.
    uploader.set_thumbnail.assert_called_once_with(UPLOADED["video_id"], auto_cover)


# --- The hard publication gate keeps blocking when the report is missing.


def test_gate_still_blocks_upload_when_report_is_missing(tmp_path):
    bundle = tmp_path / "evidence.json"
    bundle.write_text(json.dumps({
        "schema_version": "sandboxer.evidence-bundle.v1",
        "version": 1,
        "url": "sandboxer://evidence/s/v1",
        "previous_version_url": None,
        "previous_bundle_hash": None,
        "provenance": ["sandboxer://evidence/s/v1"],
        "public": {"specification": {"series_id": "Series 001 Match 1"}},
        "restricted": {},
        "checksums": {},
        "bundle_hash": "a" * 64,
        "signature": "b" * 64,
    }), encoding="utf-8")
    uploader = YoutubeUploader(service=FakeYoutubeService(), dry_run=False)
    with pytest.raises(PublishGateError, match="PUBLISH_GATE_BLOCKED.*PUBLISH_GATE_REPORT_URL_MISSING"):
        uploader.upload(
            _video(tmp_path),
            title="t",
            description="d",
            approved=True,
            bundle_path=bundle,
            report_url=None,
            publications_index={"publications": [{"id": "series-001-match-1"}]},
            require_http=False,
        )


def test_gate_collects_missing_report_rule_standalone(tmp_path):
    failures = check_publish_gate(None, None, None, require_http=False)
    assert "PUBLISH_GATE_REPORT_URL_MISSING" in failures
