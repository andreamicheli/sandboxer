from __future__ import annotations

import io
import wave

import pytest

from sandboxer_v0.tts import (
    DEFAULT_TTS_MODEL,
    FakeTtsAdapter,
    GeminiTtsAdapter,
    TtsError,
    render_commentary_audio,
)
from sandboxer_v0.video import TtsPreflight, VideoError, build_video_manifest


def _replay():
    return {"schema_version": "sandboxer.replay.v1", "source_bundle_hash": "a" * 64, "panes": [{"identity": "DeepSeek V4 Pro"}, {"identity": "MiMo V2.5 Pro"}], "layout": {"split": {"left": .5, "right": .5, "permanent": True}}, "frames": [
        {"sequence": 1, "at_monotonic_ns": 0, "event_id": "e1", "event_type": "MATCH_STARTED", "phase": "blue", "pane": 0, "text": "defend the service"},
        {"sequence": 2, "at_monotonic_ns": 2_000_000_000, "event_id": "e2", "event_type": "MODEL_RESPONSE", "phase": "blue", "pane": 1, "text": "inspect the opponent"},
        {"sequence": 3, "at_monotonic_ns": 4_000_000_000, "event_id": "e3", "event_type": "MATCH_FINISHED", "phase": "finalizing", "pane": 0, "text": ""},
    ]}


def _manifest():
    return build_video_manifest(_replay(), report={"report_url": "sandboxer://reports/s/v1", "outcome": {"winner": "DeepSeek V4 Pro"}}, model_metadata={"DeepSeek V4 Pro": {"producer": "DeepSeek"}, "MiMo V2.5 Pro": {"producer": "Xiaomi"}}, benchmark_snapshot={})


def test_fake_adapter_synthesis_is_deterministic_wav_within_bounds():
    adapter = FakeTtsAdapter()
    first = adapter.synthesize(script="The service is still healthy.", voice="Kore", style={"pace": "medium"})
    second = adapter.synthesize(script="The service is still healthy.", voice="Kore", style={"pace": "medium"})
    assert first.audio == second.audio
    with wave.open(io.BytesIO(first.audio), "rb") as reader:
        assert reader.getframerate() == 24000 and reader.getnchannels() == 1 and reader.getsampwidth() == 2
    assert 300 <= first.duration_ms <= 30_000
    assert len(first.record["audio_sha256"]) == 64 and len(first.record["script_hash"]) == 64


def test_fake_preflight_passes_and_drift_fails_closed():
    expected = TtsPreflight(DEFAULT_TTS_MODEL, ("Kore", "Charon"), "settings-v1")
    FakeTtsAdapter().preflight(expected).verify()
    with pytest.raises(VideoError, match="TTS_PREFLIGHT_DRIFT"):
        FakeTtsAdapter(model="other-model").preflight(expected).verify()


def test_render_commentary_maps_roles_to_pinned_voices_and_hashes_blocks(tmp_path):
    manifest = _manifest()
    rendered = render_commentary_audio(manifest, FakeTtsAdapter(), out_dir=tmp_path)
    assert rendered["block_count"] == len(manifest["commentary"])
    assert rendered["voices"] == {"play_by_play": "Kore", "analyst": "Charon"}
    assert rendered["expected"]["model"] == "gemini-3.1-flash-tts-preview"
    for block in rendered["blocks"]:
        assert (tmp_path / block["file"]).is_file()
        assert block["start_frame"] is not None
    assert all(block["voice"] in {"Kore", "Charon"} for block in rendered["blocks"])
    again = render_commentary_audio(manifest, FakeTtsAdapter(), out_dir=tmp_path)
    assert again["blocks_hash"] == rendered["blocks_hash"]


def test_render_commentary_fails_on_model_drift(tmp_path):
    with pytest.raises(VideoError, match="TTS_PREFLIGHT_DRIFT"):
        render_commentary_audio(_manifest(), FakeTtsAdapter(model="not-the-pinned-model"), out_dir=tmp_path)


def test_gemini_adapter_needs_control_plane_key_and_unsupported_voice_fails():
    adapter = GeminiTtsAdapter(probe=False)
    with pytest.raises(TtsError, match="TTS_CREDENTIAL_MISSING"):
        adapter.synthesize(script="hello", voice="Kore", style={})
    with pytest.raises(TtsError, match="TTS_VOICE_UNSUPPORTED"):
        FakeTtsAdapter().synthesize(script="hello", voice="Nobody", style={})


def test_gemini_preflight_without_probe_checks_drift_without_network():
    expected = TtsPreflight("gemini-3.1-flash-tts-preview", ("Kore", "Charon"), "settings-v1")
    GeminiTtsAdapter(probe=False).preflight(expected).verify()
    with pytest.raises(VideoError, match="TTS_PREFLIGHT_DRIFT"):
        GeminiTtsAdapter(probe=False, model="drifted").preflight(expected).verify()
