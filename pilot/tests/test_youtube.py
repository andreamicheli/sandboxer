from __future__ import annotations

from sandboxer_v0.video import build_video_manifest
from sandboxer_v0.youtube import FakeYoutubeService, YoutubeError, YoutubeUploader, youtube_metadata


def _replay():
    return {"schema_version": "sandboxer.replay.v1", "source_bundle_hash": "a" * 64, "panes": [{"identity": "DeepSeek V4 Pro"}, {"identity": "MiMo V2.5 Pro"}], "layout": {"split": {"left": .5, "right": .5, "permanent": True}}, "frames": [
        {"sequence": 1, "at_monotonic_ns": 0, "event_id": "e1", "event_type": "MATCH_STARTED", "phase": "blue", "pane": 0, "text": "defend"},
        {"sequence": 2, "at_monotonic_ns": 2_000_000_000, "event_id": "e2", "event_type": "MODEL_RESPONSE", "phase": "blue", "pane": 1, "text": "inspect"},
        {"sequence": 3, "at_monotonic_ns": 4_000_000_000, "event_id": "e3", "event_type": "MATCH_FINISHED", "phase": "finalizing", "pane": 0, "text": ""},
    ]}


def _manifest():
    return build_video_manifest(_replay(), report={"report_url": "sandboxer://reports/s/v1", "outcome": {"winner": "DeepSeek V4 Pro"}}, model_metadata={}, benchmark_snapshot={})


def _video(tmp_path):
    video = tmp_path / "delivery.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42" * 64)
    return video


def test_fake_service_upload_runs_the_real_path_and_records_calls(tmp_path):
    service = FakeYoutubeService()
    uploader = YoutubeUploader(service=service, dry_run=False)
    result = uploader.upload(_video(tmp_path), title="Sandboxer match", description="Simulated CTF episode", approved=True)
    assert result["video_id"] == "sbx-video-0001"
    assert result["url"] == "https://www.youtube.com/watch?v=sbx-video-0001"
    assert result["dry_run"] is False and result["privacy_status"] == "unlisted"
    assert service.calls[0]["method"] == "videos.insert"


def test_real_upload_fails_closed_without_human_approval(tmp_path):
    uploader = YoutubeUploader(service=FakeYoutubeService(), dry_run=False)
    try:
        uploader.upload(_video(tmp_path), title="Sandboxer match", description="Simulated CTF episode", approved=False)
    except YoutubeError as error:
        assert "YOUTUBE_NOT_APPROVED" in str(error)
    else:
        raise AssertionError("expected YOUTUBE_NOT_APPROVED")


def test_dry_run_upload_is_deterministic_and_credential_free(tmp_path):
    video = _video(tmp_path)
    first = YoutubeUploader(dry_run=True).upload(video, title="t", description="d", approved=False)
    second = YoutubeUploader(dry_run=True).upload(video, title="t", description="d", approved=False)
    assert first["video_id"] == second["video_id"] and first["video_id"].startswith("sbx-dryrun-")
    assert first["dry_run"] is True


def test_preflight_rejects_missing_video_and_reports_channel(tmp_path):
    uploader = YoutubeUploader(service=FakeYoutubeService(), dry_run=False)
    try:
        uploader.preflight(video_path=tmp_path / "nope.mp4")
    except YoutubeError as error:
        assert "YOUTUBE_VIDEO_MISSING" in str(error)
    else:
        raise AssertionError("expected YOUTUBE_VIDEO_MISSING")
    observed = uploader.preflight(video_path=_video(tmp_path))
    assert observed["channel"]["id"] == "sbx-channel"


def test_metadata_builder_has_disclaimer_winner_chapters_and_safe_status():
    manifest = _manifest()
    report = {"title": "DeepSeek V4 Pro vs MiMo V2.5 Pro", "report_url": "https://sandboxer.example/reports/s/v1", "outcome": {"winner": "DeepSeek V4 Pro"}}
    metadata = youtube_metadata(manifest, report)
    snippet, status = metadata["snippet"], metadata["status"]
    assert "DeepSeek V4 Pro" in snippet["title"] and len(snippet["title"]) <= 100
    description = snippet["description"]
    assert "simulated capture-the-flag" in description
    assert "Series winner: DeepSeek V4 Pro" in description
    assert "Chapters:" in description and "Match 1" in description
    assert "https://sandboxer.example/reports/s/v1" in description
    assert status["privacyStatus"] == "unlisted" and status["madeForKids"] is False and status["selfDeclaredMadeForKids"] is False


def test_thumbnail_captions_and_playlist_are_recorded(tmp_path):
    service = FakeYoutubeService()
    uploader = YoutubeUploader(service=service, dry_run=False)
    video = _video(tmp_path)
    thumb = tmp_path / "thumb.png"
    thumb.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    captions = tmp_path / "captions.vtt"
    captions.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nHello\n")
    uploaded = uploader.upload(video, title="t", description="d", approved=True)
    uploader.set_thumbnail(uploaded["video_id"], thumb)
    caption = uploader.upload_captions(uploaded["video_id"], captions)
    uploader.add_to_playlist("PL-sbx", uploaded["video_id"])
    methods = [call["method"] for call in service.calls]
    assert methods == ["videos.insert", "thumbnails.set", "captions.insert", "playlistItems.insert"]
    assert caption["is_draft"] is True


def test_captions_body_includes_required_name_field(tmp_path):
    service = FakeYoutubeService()
    uploader = YoutubeUploader(service=service, dry_run=False)
    video = _video(tmp_path)
    captions = tmp_path / "captions.vtt"
    captions.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nHello\n")
    uploaded = uploader.upload(video, title="t", description="d", approved=True)
    uploader.upload_captions(uploaded["video_id"], captions)
    body = service.calls[-1]["args"]["body"]["snippet"]
    assert body["name"] == "" and body["videoId"] == uploaded["video_id"] and body["language"] == "en"
