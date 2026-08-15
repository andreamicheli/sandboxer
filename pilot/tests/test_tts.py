from __future__ import annotations

import base64
import io
import wave
from unittest import mock

import pytest

from sandboxer_v0.tts import (
    DEFAULT_FISH_TTS_MODEL,
    DEFAULT_TTS_MODEL,
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
    assert rendered["expected"]["model"] == "gemini-3.1-flash-tts-preview"


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
