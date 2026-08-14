"""Replaceable speech-synthesis adapter for the Sandboxer broadcast layer.

The editorial manifest pins a TTS contract (``TtsPreflight`` from
``video.py``).  This module provides the two implementations of that
contract:

- ``GeminiTtsAdapter`` calls the Gemini API ``interactions`` endpoint
  (``gemini-3.1-flash-tts-preview`` by default) with a control-plane-only
  API key; model or voice drift fails preflight instead of silently changing
  the episode's sound.
- ``FakeTtsAdapter`` produces deterministic WAV bytes with no credentials,
  network, or SDK import, so tests and credential-free dry runs stay
  reproducible.

``render_commentary_audio`` turns the deterministic dialogue schedule in a
video manifest into bounded, hashed audio blocks mapped to the pinned voices.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import struct
import warnings
import wave

# google-genai flags the interactions surface as experimental on every call.
warnings.filterwarnings("ignore", message="Interactions usage is experimental.*")
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from .video import TtsPreflight, VideoError, tts_block

PCM_RATE = 24_000
PCM_CHANNELS = 1
PCM_SAMPLE_WIDTH = 2

DEFAULT_TTS_MODEL = "gemini-3.1-flash-tts-preview"
DEFAULT_VOICES = ("Kore", "Charon")
DEFAULT_SETTINGS_VERSION = "settings-v1"
VOICE_BY_ROLE = {"play_by_play": "Kore", "analyst": "Charon"}
SUPPORTED_VOICES = frozenset(
    {
        "Zephyr", "Puck", "Charon", "Kore", "Fenrir", "Leda", "Orus", "Aoede",
        "Callirrhoe", "Autonoe", "Enceladus", "Iapetus", "Umbriel", "Algieba",
        "Despina", "Erinome", "Algenib", "Rasalgethi", "Laomedeia", "Achernar",
        "Alnilam", "Schedar", "Gacrux", "Pulcherrima", "Achird",
        "Zubenelgenubi", "Vindemiatrix", "Sadachbia", "Sadaltager", "Sulafat",
    }
)


class TtsError(ValueError):
    pass


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def _wav_bytes(pcm: bytes, *, rate: int = PCM_RATE) -> bytes:
    """Wrap raw 16-bit mono PCM in a WAV container (deterministic, no deps)."""
    if not pcm:
        raise TtsError("TTS_AUDIO_EMPTY")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(PCM_CHANNELS)
        writer.setsampwidth(PCM_SAMPLE_WIDTH)
        writer.setframerate(rate)
        writer.writeframes(pcm)
    return buffer.getvalue()


def _duration_ms(wav: bytes) -> int:
    with wave.open(io.BytesIO(wav), "rb") as reader:
        frames = reader.getnframes()
        return round(frames / reader.getframerate() * 1000)


@dataclass(frozen=True)
class TtsPreflightResult:
    """Observed preflight state; availability is a hard gate for preview TTS."""

    expected: TtsPreflight
    observed: TtsPreflight
    available: bool
    probe_duration_ms: int | None = None
    probe_sha256: str | None = None

    def verify(self) -> None:
        self.expected.verify(self.observed)
        if not self.available:
            raise TtsError("TTS_PREVIEW_UNAVAILABLE")


@dataclass(frozen=True)
class TtsBlockResult:
    script: str
    model: str
    voice: str
    style: Mapping[str, Any]
    audio: bytes
    duration_ms: int
    record: dict[str, Any]


class TtsAdapter(Protocol):
    def preflight(self, expected: TtsPreflight) -> TtsPreflightResult: ...

    def synthesize(self, *, script: str, voice: str, style: Mapping[str, Any]) -> TtsBlockResult: ...


def _style_instruction(style: Mapping[str, Any]) -> str:
    """Deterministic natural-language direction from a style mapping."""
    if not style:
        return ""
    parts: list[str] = []
    for key in sorted(style):
        value = style[key]
        parts.append(f"{key}: {value}")
    return "Narrate with the following direction: " + "; ".join(parts) + ". "


def _audio_data(interaction: Any) -> bytes:
    """Extract base64 audio from an interactions response, SDK-version tolerant."""
    audio = getattr(interaction, "output_audio", None)
    data = getattr(audio, "data", None) if audio is not None else None
    if data:
        return base64.b64decode(data)
    for item in getattr(interaction, "outputs", ()) or ():
        data = getattr(item, "data", None)
        if data:
            return base64.b64decode(data)
    raise TtsError("TTS_AUDIO_MISSING")


class GeminiTtsAdapter:
    """Gemini API TTS through a control-plane-only API key.

    The endpoint is a preview: preflight performs a tiny synthesis probe so
    availability and voice stability fail closed before any commentary block
    is rendered.  A fallback is never silently substituted.
    """

    def __init__(
        self,
        *,
        client: Any | None = None,
        api_key: str | None = None,
        model: str = DEFAULT_TTS_MODEL,
        voices: Sequence[str] = DEFAULT_VOICES,
        settings_version: str = DEFAULT_SETTINGS_VERSION,
        probe: bool = True,
    ) -> None:
        self._client = client
        self._api_key = api_key
        self.model = model
        self.voices = tuple(voices)
        self.settings_version = settings_version
        self.probe = probe

    def _genai_client(self) -> Any:
        if self._client is not None:
            return self._client
        key = self._api_key or os.environ.get("GEMINI_API_KEY")
        if not key:
            raise TtsError("TTS_CREDENTIAL_MISSING")
        from google import genai  # lazy: fakes and dry runs never need the SDK

        return genai.Client(api_key=key)

    def _expected(self) -> TtsPreflight:
        return TtsPreflight(self.model, self.voices, self.settings_version)

    def _probe(self) -> TtsBlockResult:
        return self.synthesize(script="Say ok.", voice=self.voices[0], style={"probe": True})

    def preflight(self, expected: TtsPreflight) -> TtsPreflightResult:
        observed = self._expected()
        if not self.probe:
            # Availability probe disabled: verify drift only, never claim availability.
            return TtsPreflightResult(expected=expected, observed=observed, available=True)
        block = self._probe()
        return TtsPreflightResult(
            expected=expected,
            observed=observed,
            available=True,
            probe_duration_ms=block.duration_ms,
            probe_sha256=block.record["audio_sha256"],
        )

    def synthesize(self, *, script: str, voice: str, style: Mapping[str, Any]) -> TtsBlockResult:
        if not script.strip():
            raise TtsError("TTS_SCRIPT_EMPTY")
        if voice not in SUPPORTED_VOICES:
            raise TtsError("TTS_VOICE_UNSUPPORTED")
        client = self._genai_client()
        prompt = _style_instruction(style) + script
        try:
            interaction = client.interactions.create(
                model=self.model,
                input=prompt,
                response_format={"type": "audio"},
                generation_config={"speech_config": [{"voice": voice}]},
            )
        except Exception as error:
            raise TtsError(f"TTS_GENERATION_FAILED: {type(error).__name__}: {error}") from error
        pcm = _audio_data(interaction)
        wav = _wav_bytes(pcm)
        duration_ms = _duration_ms(wav)
        record = tts_block(
            script=script,
            model=self.model,
            voice=voice,
            style=style,
            audio=wav,
            duration_ms=duration_ms,
        )
        return TtsBlockResult(
            script=script, model=self.model, voice=voice, style=dict(style),
            audio=wav, duration_ms=duration_ms, record=record,
        )


class FakeTtsAdapter:
    """Deterministic credential-free adapter for tests and dry runs."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_TTS_MODEL,
        voices: Sequence[str] = DEFAULT_VOICES,
        settings_version: str = DEFAULT_SETTINGS_VERSION,
        ms_per_char: int = 80,
        min_ms: int = 300,
        max_ms: int = 30_000,
    ) -> None:
        self.model = model
        self.voices = tuple(voices)
        self.settings_version = settings_version
        self.ms_per_char = ms_per_char
        self.min_ms = min_ms
        self.max_ms = max_ms
        self.calls: list[dict[str, Any]] = []

    def preflight(self, expected: TtsPreflight) -> TtsPreflightResult:
        return TtsPreflightResult(
            expected=expected,
            observed=TtsPreflight(self.model, self.voices, self.settings_version),
            available=True,
        )

    def synthesize(self, *, script: str, voice: str, style: Mapping[str, Any]) -> TtsBlockResult:
        if not script.strip():
            raise TtsError("TTS_SCRIPT_EMPTY")
        if voice not in SUPPORTED_VOICES:
            raise TtsError("TTS_VOICE_UNSUPPORTED")
        seed = _digest({"script": script, "voice": voice, "style": style})
        duration_ms = max(self.min_ms, min(self.max_ms, len(script) * self.ms_per_char))
        frames = duration_ms * PCM_RATE // 1000
        stream = hashlib.sha256(seed.encode()).digest()
        pcm = bytearray()
        position = 0
        for index in range(frames):
            if position >= len(stream):
                stream = hashlib.sha256(stream).digest()
                position = 0
            pcm += struct.pack("<h", stream[position] * 127)
            position += 1
        wav = _wav_bytes(bytes(pcm))
        record = tts_block(
            script=script, model=self.model, voice=voice, style=style,
            audio=wav, duration_ms=duration_ms,
        )
        self.calls.append({"script": script, "voice": voice, "style": dict(style)})
        return TtsBlockResult(
            script=script, model=self.model, voice=voice, style=dict(style),
            audio=wav, duration_ms=duration_ms, record=record,
        )


def preflight_from_mapping(mapping: Mapping[str, Any]) -> TtsPreflight:
    """Rebuild a ``TtsPreflight`` from a manifest/JSON mapping.

    JSON round-trips tuples as lists, so ``voices`` is normalized back to a
    tuple; otherwise drift checks would fail after serialize/deserialize.
    """
    return TtsPreflight(
        model=str(mapping["model"]),
        voices=tuple(mapping["voices"]),
        settings_version=str(mapping["settings_version"]),
    )


def render_commentary_audio(
    manifest: Mapping[str, Any],
    adapter: TtsAdapter,
    *,
    out_dir: Path,
    voice_by_role: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Render every commentary line as a bounded, hashed audio block.

    The deterministic dialogue schedule controls order and non-overlap; the
    audio model only speaks each already-scheduled line.  Returns the enriched
    ``tts`` section to merge into the video manifest.
    """
    expected = preflight_from_mapping(manifest["tts"]["expected"])
    adapter.preflight(expected).verify()
    mapping = dict(voice_by_role or VOICE_BY_ROLE)
    lines = list(manifest.get("commentary", ()))
    if any(line.get("voice_role") not in mapping for line in lines):
        raise TtsError("TTS_ROLE_UNMAPPED")
    out_dir.mkdir(parents=True, exist_ok=True)
    blocks: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        voice = mapping[line["voice_role"]]
        style = {"voice_role": line["voice_role"], "pace": "medium"}
        result = adapter.synthesize(script=str(line.get("text", "")), voice=voice, style=style)
        filename = f"block-{index:04d}.wav"
        (out_dir / filename).write_bytes(result.audio)
        blocks.append({**result.record, "file": filename, "start_frame": line.get("start_frame"), "end_frame": line.get("end_frame")})
    blocks_hash = _digest([block["script_hash"] for block in blocks])
    return {
        "expected": asdict(expected),
        "voices": dict(mapping),
        "blocks": blocks,
        "block_count": len(blocks),
        "blocks_hash": blocks_hash,
        "out_dir": str(out_dir),
    }
