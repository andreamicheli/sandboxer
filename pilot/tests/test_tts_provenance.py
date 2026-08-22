"""TTS provenance: requested vs observed must both survive into the manifest.

Regression for the run where the video-manifest kept the pinned
``gemini-3.1-flash-tts-preview`` contract while the broadcast record registered
Fish Audio's ``s2.1-pro-free`` — a reviewer could not tell which provider
produced the audio.
"""

from __future__ import annotations

import base64
import io
import json
import wave

from sandboxer_v0.tts import (
    DEFAULT_FISH_TTS_MODEL,
    DEFAULT_GEMINI_TTS_MODEL,
    DEFAULT_TTS_MODEL,
    FakeTtsAdapter,
    FishAudioTtsAdapter,
    GeminiTtsAdapter,
    render_commentary_audio,
)
from sandboxer_v0.video import (
    DEFAULT_TTS_PROVIDER,
    TtsProvenance,
    build_video_manifest,
    provenance_drift,
    tts_provenance_section,
    validate_provenance,
)


def _replay():
    return {"schema_version": "sandboxer.replay.v1", "source_bundle_hash": "a" * 64, "panes": [{"identity": "DeepSeek V4 Pro"}, {"identity": "MiMo V2.5 Pro"}], "layout": {"split": {"left": .5, "right": .5, "permanent": True}}, "frames": [
        {"sequence": 1, "at_monotonic_ns": 0, "event_id": "e1", "event_type": "MATCH_STARTED", "phase": "blue", "pane": 0, "text": "defend"},
        {"sequence": 2, "at_monotonic_ns": 2_000_000_000, "event_id": "e2", "event_type": "MODEL_RESPONSE", "phase": "blue", "pane": 1, "text": "inspect"},
        {"sequence": 3, "at_monotonic_ns": 4_000_000_000, "event_id": "e3", "event_type": "MATCH_FINISHED", "phase": "finalizing", "pane": 0, "text": ""},
    ]}


def _manifest(**kwargs):
    return build_video_manifest(
        _replay(), report={"report_url": "r", "outcome": {}},
        model_metadata={}, benchmark_snapshot={}, **kwargs,
    )


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

    def tts(self, *, text: str, reference_id: str) -> bytes:
        return self.data


class _Interaction:
    output_audio = type("Audio", (), {"data": base64.b64encode(b"\x00\x00" * 2400)})()


class _GeminiClient:
    def __init__(self, working_model: str) -> None:
        self.working_model = working_model

    @property
    def interactions(self) -> "_GeminiClient":
        return self

    def create(self, **kwargs: object) -> _Interaction:
        if str(kwargs["model"]) != self.working_model:
            raise RuntimeError("Error code: 429 - quota exceeded")
        return _Interaction()


def test_requested_and_observed_both_present_with_drift_flag(tmp_path):
    # The fake adapter emulates the default (Fish) provider exactly.
    rendered = render_commentary_audio(
        _manifest(), FakeTtsAdapter(provider=DEFAULT_TTS_PROVIDER), out_dir=tmp_path
    )
    assert set(rendered["requested"]) == {"provider", "model", "voices"}
    assert set(rendered["observed"]) == {"provider", "model", "voices"}
    assert rendered["tts_provider_drift"] is False
    assert validate_provenance({"tts": rendered}) == []


def test_drift_sets_flag_and_validate_reports_nothing(tmp_path):
    # The incident: manifest pinned one contract, Fish Audio actually produced
    # the audio.  With Fish now the default pin, drift is exercised via a
    # Gemini-pinned requested section instead.
    manifest = _manifest()
    manifest["tts"]["expected"] = {
        "model": DEFAULT_GEMINI_TTS_MODEL, "voices": ["Kore", "Charon"],
        "settings_version": "settings-v1",
    }
    manifest["tts"]["requested"] = {
        "provider": "gemini", "model": DEFAULT_GEMINI_TTS_MODEL, "voices": ["Kore", "Charon"],
    }
    adapter = FishAudioTtsAdapter(
        client=_FishClient(_fish_wav()), reference_ids={"Kore": "ref-k", "Charon": "ref-c"}, probe=False
    )
    rendered = render_commentary_audio(manifest, adapter, out_dir=tmp_path)
    assert rendered["requested"] == {"provider": "gemini", "model": DEFAULT_GEMINI_TTS_MODEL, "voices": ("Kore", "Charon")}
    assert rendered["observed"]["provider"] == "fish"
    assert rendered["observed"]["model"] == DEFAULT_FISH_TTS_MODEL
    assert rendered["models_used"] == [DEFAULT_FISH_TTS_MODEL]
    assert rendered["tts_provider_drift"] is True
    problems = validate_provenance({"tts": rendered})
    assert problems == [], problems


def test_gemini_fallback_records_observed_model_not_requested(tmp_path):
    adapter = GeminiTtsAdapter(
        client=_GeminiClient(working_model="gemini-2.5-flash-preview-tts"), probe=False,
        allow_fallback=True, fallback_models=("gemini-2.5-flash-preview-tts",), retries=0,
    )
    result = adapter.synthesize(script="hello", voice="Kore", style={})
    assert result.model == "gemini-2.5-flash-preview-tts"
    assert adapter.observed_provenances[-1] == TtsProvenance(
        provider="gemini", model="gemini-2.5-flash-preview-tts", voices=("Kore",)
    )


def test_missing_observed_is_reported():
    fresh = _manifest()  # built before synthesis: nothing observed yet
    assert fresh["tts"]["requested"]["model"] == DEFAULT_TTS_MODEL
    assert fresh["tts"]["observed"] is None
    assert "TTS_PROVENANCE_OBSERVED_MISSING" in validate_provenance(fresh)
    stripped = {"tts": {"requested": {"provider": "gemini", "model": "m"}}}
    assert "TTS_PROVENANCE_OBSERVED_MISSING" in validate_provenance(stripped)


def test_empty_observed_model_is_reported():
    section = tts_provenance_section(
        TtsProvenance("fake", "m", ("Kore",)), TtsProvenance("fake", "", ("Kore",))
    )
    problems = validate_provenance({"tts": section})
    assert "TTS_PROVENANCE_OBSERVED_MODEL_EMPTY" in problems


def test_inconsistent_flags_are_reported():
    requested = TtsProvenance("gemini", DEFAULT_TTS_MODEL, ("Kore", "Charon"))
    observed = TtsProvenance("fish", DEFAULT_FISH_TTS_MODEL, ("Kore", "Charon"))
    # requested==observed but drift flag true.
    equal = tts_provenance_section(requested, requested)
    equal["tts_provider_drift"] = True
    assert "TTS_PROVENANCE_DRIFT_FLAG_INCONSISTENT" in validate_provenance({"tts": equal})
    # drift flag false but requested!=observed.
    differing = tts_provenance_section(requested, observed)
    differing["tts_provider_drift"] = False
    assert "TTS_PROVENANCE_DRIFT_FLAG_UNSET" in validate_provenance({"tts": differing})


def test_unspoken_voice_is_not_drift_but_unrequested_voice_is():
    requested = {"provider": "gemini", "model": "m", "voices": ["Kore", "Charon"]}
    # A short episode that never reaches the analyst role did not drift.
    assert provenance_drift(requested, {"provider": "gemini", "model": "m", "voices": ["Kore"]}) is False
    # An unrequested voice (or provider/model) is drift.
    assert provenance_drift(requested, {"provider": "gemini", "model": "m", "voices": ["Kore", "Puck"]}) is True
    assert provenance_drift(requested, {"provider": "fish", "model": "m", "voices": ["Kore"]}) is True


def test_consistent_case_passes_validation(tmp_path):
    rendered = render_commentary_audio(
        _manifest(), FakeTtsAdapter(provider=DEFAULT_TTS_PROVIDER), out_dir=tmp_path
    )
    assert rendered["requested"]["provider"] == "fish"
    assert rendered["observed"]["provider"] == "fish"
    assert rendered["observed"]["model"] == DEFAULT_TTS_MODEL
    assert rendered["observed"]["voices"] == ("Kore",)
    assert rendered["tts_provider_drift"] is False
    assert validate_provenance({"tts": rendered}) == []


def test_json_round_trip_keeps_provenance_valid(tmp_path):
    rendered = render_commentary_audio(
        _manifest(),
        FishAudioTtsAdapter(client=_FishClient(_fish_wav()), reference_ids={"Kore": "ref-k"}, probe=False),
        out_dir=tmp_path,
    )
    reloaded = json.loads(json.dumps({"tts": rendered}))
    assert reloaded["tts"]["requested"]["voices"] == ["Kore", "Charon"]
    assert reloaded["tts"]["observed"]["model"] == DEFAULT_FISH_TTS_MODEL
    assert reloaded["tts"]["tts_provider_drift"] is False
    assert validate_provenance(reloaded) == []


def test_manifest_carries_requested_before_synthesis():
    manifest = _manifest()
    assert manifest["tts"]["requested"] == {
        "provider": "fish", "model": DEFAULT_TTS_MODEL, "voices": ("Kore", "Charon"),
    }
    assert manifest["tts"]["observed"] is None
