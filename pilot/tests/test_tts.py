from __future__ import annotations

import base64
import io
import wave
from unittest import mock

import pytest

from sandboxer_v0.tts import (
    DEFAULT_FISH_TTS_MODEL,
    DEFAULT_TTS_MODEL,
    DEFAULT_TTS_SETTINGS_VERSION,
    FakeTtsAdapter,
    FishAudioTtsAdapter,
    GeminiTtsAdapter,
    TtsError,
    TtsPreflightResult,
    parse_voice_spec,
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
    # The fake adapter defaults to the manifest's default (Fish) contract.
    expected = TtsPreflight(DEFAULT_TTS_MODEL, ("Kore", "Charon"), DEFAULT_TTS_SETTINGS_VERSION)
    FakeTtsAdapter().preflight(expected).verify()
    with pytest.raises(VideoError, match="TTS_PREFLIGHT_DRIFT"):
        FakeTtsAdapter(model="other-model").preflight(expected).verify()


def test_render_commentary_maps_roles_to_pinned_voices_and_hashes_blocks(tmp_path):
    manifest = _manifest()
    rendered = render_commentary_audio(manifest, FakeTtsAdapter(), out_dir=tmp_path)
    assert rendered["block_count"] == len(manifest["commentary"])
    assert rendered["voices"] == {"play_by_play": "Kore", "analyst": "Charon"}
    assert rendered["expected"]["model"] == DEFAULT_FISH_TTS_MODEL
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


def test_gemini_adapter_retries_transient_quota_then_succeeds():
    class _Interaction:
        output_audio = type("Audio", (), {"data": base64.b64encode(b"\x00\x00" * 2400)})()

    class _FlakyClient:
        def __init__(self) -> None:
            self.calls = 0

        @property
        def interactions(self) -> "_FlakyClient":
            return self

        def create(self, **kwargs: object) -> _Interaction:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("Error code: 429 - quota exceeded, please retry in 37s")
            return _Interaction()

    import time

    client = _FlakyClient()
    adapter = GeminiTtsAdapter(client=client, probe=False, retries=2, retry_base_seconds=0.01)
    with mock.patch.object(time, "sleep") as sleep:
        result = adapter.synthesize(script="hello", voice="Kore", style={})
    assert client.calls == 2
    assert sleep.call_count == 1
    assert result.duration_ms > 0


def test_gemini_adapter_gives_up_after_retries():
    class _BrokenClient:
        @property
        def interactions(self) -> "_BrokenClient":
            return self

        def create(self, **kwargs: object) -> object:
            raise RuntimeError("Error code: 429 - quota exceeded")

    adapter = GeminiTtsAdapter(
        client=_BrokenClient(), probe=False, retries=1, retry_base_seconds=0.01, chain_retries=0
    )
    with pytest.raises(TtsError, match="TTS_GENERATION_FAILED.*429"):
        adapter.synthesize(script="hello", voice="Kore", style={})


def _interaction():
    return type("I", (), {"output_audio": type("A", (), {"data": base64.b64encode(b"\x00\x00" * 2400)})()})()


def test_fallback_chain_continues_on_quota_of_primary_model():
    class _ChainClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        @property
        def interactions(self) -> "_ChainClient":
            return self

        def create(self, **kwargs: object) -> object:
            model = str(kwargs["model"])
            self.calls.append(model)
            if model == "gemini-3.1-flash-tts-preview":
                raise RuntimeError("Error code: 429 - quota exceeded for primary")
            return _interaction()

    client = _ChainClient()
    adapter = GeminiTtsAdapter(
        client=client, probe=False, allow_fallback=True,
        fallback_models=("gemini-2.5-flash-preview-tts",),
        retries=0,
    )
    result = adapter.synthesize(script="hello", voice="Kore", style={})
    assert client.calls == ["gemini-3.1-flash-tts-preview", "gemini-2.5-flash-preview-tts"]
    assert result.model == "gemini-2.5-flash-preview-tts"


def test_fallback_not_used_without_approval():
    class _ChainClient:
        @property
        def interactions(self) -> "_ChainClient":
            return self

        def create(self, **kwargs: object) -> object:
            raise RuntimeError("Error code: 429 - quota exceeded")

    adapter = GeminiTtsAdapter(
        client=_ChainClient(), probe=False, allow_fallback=False,
        retries=0, chain_retries=0,
    )
    with pytest.raises(TtsError, match="TTS_GENERATION_FAILED.*429"):
        adapter.synthesize(script="hello", voice="Kore", style={})


def test_permanent_error_never_masked_by_fallback():
    class _ChainClient:
        @property
        def interactions(self) -> "_ChainClient":
            return self

        def create(self, **kwargs: object) -> object:
            raise RuntimeError("Error code: 400 - Input blocked by policy")

    adapter = GeminiTtsAdapter(
        client=_ChainClient(), probe=False, allow_fallback=True,
        fallback_models=("gemini-2.5-flash-preview-tts",),
        retries=0,
    )
    with pytest.raises(TtsError, match="TTS_GENERATION_FAILED.*400"):
        adapter.synthesize(script="hello", voice="Kore", style={})


def test_preflight_verify_allows_approved_fallback_drift():
    expected = TtsPreflight("gemini-3.1-flash-tts-preview", ("Kore", "Charon"), "settings-v1")
    observed = TtsPreflight("gemini-2.5-flash-preview-tts", ("Kore", "Charon"), "settings-v1")
    result = TtsPreflightResult(expected=expected, observed=observed, available=True)
    result.verify(allow_fallback=True)
    with pytest.raises(VideoError, match="TTS_PREFLIGHT_DRIFT"):
        result.verify(allow_fallback=False)


def test_render_commentary_resume_rerenders_when_script_changes(tmp_path):
    def manifest_for(text: str):
        drafted = [{"voice_role": "play_by_play", "line_type": "observed", "event_ids": ["e1"], "text": text}]
        return build_video_manifest(_replay(), report={"report_url": "r", "outcome": {}}, model_metadata={}, benchmark_snapshot={}, commentary=drafted)

    render_commentary_audio(manifest_for("First line."), FakeTtsAdapter(), out_dir=tmp_path)
    second = FakeTtsAdapter()
    render_commentary_audio(manifest_for("Changed line."), second, out_dir=tmp_path)
    # The script changed, so the stale block must be re-rendered, not reused.
    assert [call["script"] for call in second.calls] == ["Changed line."]


def test_render_commentary_resumes_existing_blocks(tmp_path):
    manifest = _manifest()
    first = render_commentary_audio(manifest, FakeTtsAdapter(), out_dir=tmp_path)
    # Second pass must not re-synthesize: blocks and models are reused.
    adapter = FakeTtsAdapter()
    second = render_commentary_audio(manifest, adapter, out_dir=tmp_path)
    assert adapter.calls == []
    assert second["blocks_hash"] == first["blocks_hash"]
    assert set(second["models_used"]) <= set(first["models_used"])


def _fish_wav(duration_ms: int = 100) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(24000)
        writer.writeframes(b"\x00\x00" * (24000 * duration_ms // 1000))
    return buffer.getvalue()


class _FishClient:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.calls: list[dict[str, str]] = []

    def tts(self, *, text: str, reference_id: str) -> bytes:
        self.calls.append({"text": text, "reference_id": reference_id})
        return self.data


def test_fish_adapter_synthesizes_wav_and_records_model():
    client = _FishClient(_fish_wav(120))
    adapter = FishAudioTtsAdapter(
        client=client, reference_ids={"Kore": "ref-k", "Charon": "ref-c"}, probe=False
    )
    result = adapter.synthesize(script="hello", voice="Kore", style={})
    assert result.model == DEFAULT_FISH_TTS_MODEL
    assert result.duration_ms > 0
    assert client.calls == [{"text": "hello", "reference_id": "ref-k"}]
    assert len(result.record["audio_sha256"]) == 64


def test_fish_adapter_fails_closed_on_unmapped_voice():
    adapter = FishAudioTtsAdapter(client=_FishClient(_fish_wav()), reference_ids={"Kore": "ref-k"}, probe=False)
    with pytest.raises(TtsError, match="TTS_VOICE_UNMAPPED"):
        adapter.synthesize(script="hello", voice="Charon", style={})


def test_fish_adapter_needs_credential_without_client():
    adapter = FishAudioTtsAdapter(probe=False, reference_ids={"Kore": "ref-k"})
    with pytest.raises(TtsError, match="TTS_CREDENTIAL_MISSING"):
        adapter.synthesize(script="hello", voice="Kore", style={})


def test_fish_adapter_retries_transient_quota_then_succeeds():
    class _FlakyFish:
        def __init__(self) -> None:
            self.calls = 0

        def tts(self, *, text: str, reference_id: str) -> bytes:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("HTTP Error 429: too many requests")
            return _fish_wav(100)

    import time

    client = _FlakyFish()
    adapter = FishAudioTtsAdapter(client=client, reference_ids={"Kore": "ref-k"}, probe=False, retries=2, retry_base_seconds=0.01)
    with mock.patch.object(time, "sleep") as sleep:
        result = adapter.synthesize(script="hello", voice="Kore", style={})
    assert client.calls == 2
    assert sleep.call_count == 1
    assert result.duration_ms > 0


def test_fish_preflight_approves_drift_and_reports_fish_model():
    expected = TtsPreflight("gemini-3.1-flash-tts-preview", ("Kore", "Charon"), "settings-v1")
    adapter = FishAudioTtsAdapter(client=_FishClient(_fish_wav()), reference_ids={"Kore": "ref-k", "Charon": "ref-c"})
    result = adapter.preflight(expected)
    result.verify(allow_fallback=True)
    assert result.observed.model == DEFAULT_FISH_TTS_MODEL


def test_render_commentary_with_fish_adapter_records_models_used(tmp_path):
    manifest = _manifest()
    adapter = FishAudioTtsAdapter(client=_FishClient(_fish_wav()), reference_ids={"Kore": "ref-k", "Charon": "ref-c"})
    rendered = render_commentary_audio(manifest, adapter, out_dir=tmp_path)
    assert rendered["block_count"] == len(manifest["commentary"])
    assert rendered["models_used"] == [DEFAULT_FISH_TTS_MODEL]
    assert rendered["expected"]["model"] == DEFAULT_FISH_TTS_MODEL


def test_parse_voice_spec_parses_pairs_and_ignores_junk():
    assert parse_voice_spec("Kore=a,Charon=b") == {"Kore": "a", "Charon": "b"}
    assert parse_voice_spec("") == {}
    assert parse_voice_spec("bad,noequals,Kore=x") == {"Kore": "x"}


def _streaming_wav(duration_ms: int = 1500) -> bytes:
    """A WAV whose RIFF/data sizes are the streaming placeholder 0xFFFFFFFF.

    Fish Audio streams audio this way: the header frame count is garbage, but
    the payload is real.  ``wave.getnframes()`` reports a huge value; the real
    length must be derived from the bytes present.
    """
    frames = 24000 * duration_ms // 1000
    pcm = b"\x00\x00" * frames
    header = (
        b"RIFF"
        + (0xFFFFFFFF).to_bytes(4, "little")
        + b"WAVE"
        + b"fmt "
        + (16).to_bytes(4, "little")
        + (1).to_bytes(2, "little")          # PCM
        + (1).to_bytes(2, "little")          # mono
        + (24000).to_bytes(4, "little")      # sample rate
        + (48000).to_bytes(4, "little")      # byte rate
        + (2).to_bytes(2, "little")          # block align
        + (16).to_bytes(2, "little")         # bits per sample
        + b"data"
        + (0xFFFFFFFF).to_bytes(4, "little")
        + pcm
    )
    return header


def test_fish_adapter_computes_duration_from_streaming_wav_payload():
    adapter = FishAudioTtsAdapter(client=_FishClient(_streaming_wav(1500)), reference_ids={"Kore": "ref-k"}, probe=False)
    result = adapter.synthesize(script="hello", voice="Kore", style={})
    assert 1400 <= result.duration_ms <= 1600


def test_render_commentary_resumes_streaming_wav_blocks(tmp_path):
    manifest = _manifest()
    refs = {"Kore": "ref-k", "Charon": "ref-c"}
    first = render_commentary_audio(manifest, FishAudioTtsAdapter(client=_FishClient(_streaming_wav()), reference_ids=refs, probe=False), out_dir=tmp_path)
    second_client = _FishClient(_streaming_wav())
    second = render_commentary_audio(manifest, FishAudioTtsAdapter(client=second_client, reference_ids=refs, probe=False), out_dir=tmp_path)
    # Streaming-WAV blocks are reused; the second pass must not re-synthesize.
    assert second_client.calls == []
    assert second["blocks_hash"] == first["blocks_hash"]


def test_render_commentary_preserves_scene_anchored_intro_start(tmp_path):
    # custom_intro (8s) replaced cold_open, so intro lines anchor to
    # model_cards_and_rules; at 30fps that scene starts at frame 240, so
    # offset 4s lands at 240 + 120 = 360 frames.
    intro = [{"voice_role": "play_by_play", "line_type": "editorial", "scene": "model_cards_and_rules", "offset_seconds": 4.0, "text": "Welcome back."}]
    manifest = build_video_manifest(
        _replay(), report={"report_url": "r", "outcome": {"winner": "DeepSeek V4 Pro"}},
        model_metadata={}, benchmark_snapshot={}, intro_commentary=intro,
    )
    rendered = render_commentary_audio(manifest, FakeTtsAdapter(), out_dir=tmp_path)
    blocks = rendered["blocks"]
    # The intro block keeps its scene-anchored start (frame 360) and the
    # match block is NOT pulled earlier than its event anchor (frame 840).
    assert blocks[0]["start_frame"] == 360
    assert blocks[1]["start_frame"] == 840


def test_render_commentary_packs_blocks_by_actual_duration_no_overlap(tmp_path):
    drafted = [
        {"voice_role": "play_by_play", "line_type": "observed", "event_ids": ["e1"], "text": "A defends."},
        {"voice_role": "analyst", "line_type": "interpreted", "event_ids": ["e2"], "text": "It seems B probes."},
    ]
    manifest = build_video_manifest(
        _replay(), report={"report_url": "r", "outcome": {}}, model_metadata={},
        benchmark_snapshot={}, commentary=drafted,
    )
    refs = {"Kore": "ref-k", "Charon": "ref-c"}
    adapter = FishAudioTtsAdapter(
        client=_FishClient(_fish_wav(1500)), reference_ids=refs, probe=False
    )
    rendered = render_commentary_audio(manifest, adapter, out_dir=tmp_path)
    blocks = rendered["blocks"]
    fps = manifest["fps"]
    # Packed back-to-back by the 1.5s actual duration plus the 400ms turn gap,
    # not by the (shorter or longer) estimated window.
    expected_step = 1500 * fps // 1000 + 400 * fps // 1000
    assert blocks[1]["start_frame"] - blocks[0]["start_frame"] == expected_step
    assert blocks[0]["end_frame"] - blocks[0]["start_frame"] == 1500 * fps // 1000
    assert blocks[1]["start_frame"] >= blocks[0]["end_frame"]


def _dense_manifest(lines, *, recap_frames, fps=30):
    # Synthetic manifest with an explicit speak window: everything before the
    # final scene is speakable, the recap scene is not.
    return {
        "schema": "sandboxer.video-manifest.v1",
        "fps": fps,
        "scenes": [{"duration_frames": recap_frames}, {"duration_frames": 10 * fps}],
        "commentary": [
            {
                "voice_role": line["voice_role"],
                "text": line["text"],
                "start_frame": line.get("start_frame", index),
            }
            for index, line in enumerate(lines)
        ],
        "tts": {
            "expected": {
                "model": DEFAULT_TTS_MODEL,
                "voices": list(("Kore", "Charon")),
                "settings_version": DEFAULT_TTS_SETTINGS_VERSION,
            }
        },
    }


_DENSE_LINES = [
    {"voice_role": "play_by_play" if index % 2 == 0 else "analyst", "text": f"Line {index} of the dense series."}
    for index in range(10)
]


def test_render_commentary_shrinks_gap_when_dense_commentary_would_overflow(tmp_path):
    # 10 x 1.5s speech + fixed 400ms gaps = 18.6s in an 18s window: the old
    # fixed-gap packer raised COMMENTARY_OVERFLOW_AFTER_RENDER here.
    manifest = _dense_manifest(_DENSE_LINES, recap_frames=540)  # 18s @30fps
    adapter = FakeTtsAdapter(ms_per_char=0, min_ms=1500, max_ms=1500)
    rendered = render_commentary_audio(manifest, adapter, out_dir=tmp_path)
    blocks = rendered["blocks"]
    assert len(blocks) == 10
    boundary = 540
    for block in blocks:
        assert block["end_frame"] <= boundary
    starts = [block["start_frame"] for block in blocks]
    assert starts == sorted(starts)
    for previous, block in zip(blocks, blocks[1:]):
        assert block["start_frame"] >= previous["end_frame"]
        # Compressed gap engaged: consecutive starts are closer than the
        # duration + fixed 400ms step would ever allow.
        assert block["start_frame"] - previous["start_frame"] < 1500 * 30 // 1000 + 400 * 30 // 1000
    # Planned starts stay a lower bound (15-frame spacing here).
    for index, block in enumerate(blocks):
        assert block["start_frame"] >= _DENSE_LINES[index].get("start_frame", index)
    again = render_commentary_audio(_dense_manifest(_DENSE_LINES, recap_frames=540), adapter, out_dir=tmp_path / "again")
    assert [b["start_frame"] for b in again["blocks"]] == starts


def test_render_commentary_still_raises_when_pure_speech_overflows_window(tmp_path):
    # Speech alone (15s) exceeds the window (13s): even a zero gap cannot fit.
    manifest = _dense_manifest(_DENSE_LINES, recap_frames=390)  # 13s @30fps
    adapter = FakeTtsAdapter(ms_per_char=0, min_ms=1500, max_ms=1500)
    with pytest.raises(TtsError, match="COMMENTARY_OVERFLOW_AFTER_RENDER"):
        render_commentary_audio(manifest, adapter, out_dir=tmp_path)


def test_adapters_speak_script_verbatim_without_style_instruction():
    # ``style`` is editorial metadata, not spoken direction: the model must
    # read the commentary verbatim (regression for the spoken "Narrate with...").
    style = {"voice_role": "play_by_play", "pace": "medium"}

    fish_client = _FishClient(_fish_wav())
    FishAudioTtsAdapter(client=fish_client, reference_ids={"Kore": "ref-k"}, probe=False).synthesize(
        script="service up", voice="Kore", style=style
    )
    assert fish_client.calls[0]["text"] == "service up"

    class _Recorder:
        def __init__(self) -> None:
            self.inputs: list[str] = []

        @property
        def interactions(self) -> "_Recorder":
            return self

        def create(self, **kwargs: object) -> object:
            self.inputs.append(str(kwargs["input"]))
            return _interaction()

    recorder = _Recorder()
    GeminiTtsAdapter(client=recorder, probe=False).synthesize(script="service up", voice="Kore", style=style)
    assert recorder.inputs == ["service up"]
