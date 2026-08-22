"""BGM underlay wiring in publish_broadcast: default-on, --no-bgm, fail-closed."""
from __future__ import annotations

import argparse
import json
import subprocess
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
from sandboxer_v0.video import _digest, build_video_manifest
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


def _mock_uploader(monkeypatch) -> mock.MagicMock:
    uploader = mock.MagicMock(name="uploader")
    uploader.preflight.return_value = {"channel": {"id": "sbx-channel"}}
    uploader.upload.return_value = {
        "video_id": "sbx-video-0001",
        "url": "https://www.youtube.com/watch?v=sbx-video-0001",
        "privacy_status": "unlisted",
        "dry_run": False,
    }
    uploader.set_thumbnail.return_value = {
        "video_id": "sbx-video-0001",
        "url": "https://i.ytimg.com/vi/sbx-video-0001/hqdefault.jpg",
        "dry_run": True,
    }
    monkeypatch.setattr(pb, "YoutubeUploader", mock.MagicMock(return_value=uploader))
    return uploader


def _fake_ffmpeg(monkeypatch, duration: str = "42.5"):
    """Stub ffprobe/ffmpeg; records every command and creates output files."""
    commands: list[tuple[str, ...]] = []

    def fake_run(command, **kwargs):
        commands.append(tuple(command))
        if command[0] == "ffprobe":
            stdout = json.dumps({"format": {"duration": duration}})
        else:
            Path(command[-1]).write_bytes(b"\x00" * 64)  # non-empty output
            stdout = ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(pb.subprocess, "run", fake_run)
    return commands


def _base_argv(tmp_path: Path, video: Path) -> list[str]:
    manifest_file = tmp_path / "manifest.json"
    manifest_file.write_text(json.dumps(build_video_manifest(_replay(), report={}, model_metadata={}, benchmark_snapshot={})), encoding="utf-8")
    thumb = tmp_path / "thumb.png"
    thumb.touch()  # explicit --thumb keeps the intro-cover extraction branch off
    return [
        "--manifest", str(manifest_file),
        "--video", str(video),
        "--tts", "fake",
        "--youtube", "real",
        "--out", str(tmp_path / "broadcast.json"),
        "--thumb", str(thumb),
        "--yes",
    ]


# --- --no-bgm: the legacy path uploads --video untouched.


def test_no_bgm_uploads_the_source_video_and_records_disabled(tmp_path, monkeypatch):
    video = _video(tmp_path)
    uploader = _mock_uploader(monkeypatch)
    commands = _fake_ffmpeg(monkeypatch)

    assert pb.main([*_base_argv(tmp_path, video), "--no-bgm"]) == 0

    assert commands == []  # no ffprobe/ffmpeg delivery pipeline ran
    uploaded_path = uploader.upload.call_args.args[0]
    assert uploaded_path == video
    record = json.loads((tmp_path / "broadcast.json").read_text(encoding="utf-8"))
    assert record["bgm"] == {"enabled": False}


# --- Default: looped bed mixed through ffmpeg_delivery_commands.


def test_bgm_enabled_runs_the_delivery_pipeline_and_uploads_the_underlaid_video(tmp_path, monkeypatch):
    # Keep every ffmpeg side effect inside tmp_path: the shipped asset is
    # substituted by a stand-in so the fake bed output lands next to it.
    bgm_asset = tmp_path / "background-track.m4a"
    bgm_asset.write_bytes(b"\x00\x00\x00\x18ftypM4A " * 4)
    monkeypatch.setattr(pb, "BGM_ASSET", bgm_asset)
    video = _video(tmp_path)
    uploader = _mock_uploader(monkeypatch)
    commands = _fake_ffmpeg(monkeypatch, duration="181.025215")
    voice_track = tmp_path / "commentary-full.wav"  # default: next to the audio dir
    voice_track.write_bytes(b"RIFF")

    assert pb.main(_base_argv(tmp_path, video)) == 0

    # Two ffprobe calls (duration derivation + the pipeline's own probe), then
    # four deterministic ffmpeg commands (voice loudnorm, bed loop/trim, amix
    # mux, delivery encode).
    assert [command[0] for command in commands] == ["ffprobe", "ffprobe"] + ["ffmpeg"] * 4
    assert str(video) in commands[0] and str(video) in commands[1]
    bed = commands[3]
    assert bed[bed.index("-stream_loop") + 1] == "-1"
    assert bed[bed.index("-i") + 1] == str(bgm_asset)
    assert bed[bed.index("-t") + 1] == "181.025215"
    assert any("loudnorm=I=-28:LRA=7:TP=-9" in part for part in bed)
    assert bed[-1].endswith("background-track.m4a.underlay.wav")
    mux = commands[4]
    graph = mux[mux.index("-filter_complex") + 1]
    assert graph == "[1:a][2:a]amix=inputs=2:duration=first:weights='1 0.18'[aout]"
    assert mux[mux.index("-i") + 3] == f"{voice_track}.normalized.wav"

    delivery = video.with_name("delivery-bgm.mp4")
    assert delivery.is_file()
    assert uploader.preflight.call_args.kwargs["video_path"] == delivery
    assert uploader.upload.call_args.args[0] == delivery

    record = json.loads((tmp_path / "broadcast.json").read_text(encoding="utf-8"))
    assert record["bgm"]["enabled"] is True
    assert record["bgm"]["source"] == "assets/background-track.m4a"
    assert record["bgm"]["target_loudness_lufs"] == -28
    assert record["bgm"]["mix_weight"] == 0.18

    # Manifest provenance: composition.bgm recorded and the hash recomputed.
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))  # untouched on disk
    original_hash = manifest["manifest_hash"]
    assert record["manifest_hash"] != original_hash
    expected = dict(manifest)
    expected.pop("manifest_hash")
    expected.setdefault("composition", {})["bgm"] = {
        "enabled": True, "source": "assets/background-track.m4a",
        "target_loudness_lufs": -28, "mix_weight": 0.18,
    }
    assert record["manifest_hash"] == _digest(expected)


def test_bgm_fails_closed_when_the_voice_track_is_missing(tmp_path, monkeypatch):
    video = _video(tmp_path)
    _mock_uploader(monkeypatch)
    _fake_ffmpeg(monkeypatch)
    with pytest.raises(SystemExit, match="BGM_VOICE_TRACK_MISSING"):
        pb.main(_base_argv(tmp_path, video))


def test_bgm_fails_closed_when_the_asset_is_missing(tmp_path, monkeypatch):
    video = _video(tmp_path)
    _mock_uploader(monkeypatch)
    _fake_ffmpeg(monkeypatch)
    (tmp_path / "commentary-full.wav").write_bytes(b"RIFF")
    monkeypatch.setattr(pb, "BGM_ASSET", tmp_path / "nope.m4a")
    with pytest.raises(SystemExit, match="BGM_ASSET_MISSING"):
        pb.main(_base_argv(tmp_path, video))


def test_bgm_fails_closed_when_ffprobe_cannot_measure_the_video(tmp_path, monkeypatch):
    video = _video(tmp_path)
    _mock_uploader(monkeypatch)

    def failing_run(command, **kwargs):
        raise OSError("ffprobe missing")

    monkeypatch.setattr(pb.subprocess, "run", failing_run)
    (tmp_path / "commentary-full.wav").write_bytes(b"RIFF")
    with pytest.raises(SystemExit, match="BGM_DURATION_UNAVAILABLE"):
        pb.main(_base_argv(tmp_path, video))


def test_bgm_fails_closed_when_a_delivery_command_fails(tmp_path, monkeypatch):
    video = _video(tmp_path)
    _mock_uploader(monkeypatch)

    def failing_run(command, **kwargs):
        if command[0] == "ffmpeg":
            raise OSError("ffmpeg crashed")
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"format": {"duration": "10"}}), stderr="")

    monkeypatch.setattr(pb.subprocess, "run", failing_run)
    (tmp_path / "commentary-full.wav").write_bytes(b"RIFF")
    with pytest.raises(SystemExit, match="BGM_DELIVERY_FAILED"):
        pb.main(_base_argv(tmp_path, video))
