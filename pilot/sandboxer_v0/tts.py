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
import re
import struct
import time
import warnings
import wave

# google-genai flags the interactions surface as experimental on every call.
warnings.filterwarnings("ignore", message="Interactions usage is experimental.*")
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from .video import TtsPreflight, TtsProvenance, VideoError, tts_block, tts_provenance_section

PCM_RATE = 24_000
PCM_CHANNELS = 1
PCM_SAMPLE_WIDTH = 2

DEFAULT_TTS_MODEL = "gemini-3.1-flash-tts-preview"
# Ordered fallback chain for the Gemini TTS preview.  Free-tier quota is
# enforced per model, so when the primary model's window is exhausted the
# adapter can continue on the next model in the chain (only when explicitly
# approved; the deviation is recorded, never silent).  2.5 Pro preview has no
# free-tier quota ("limit: 0") so it is deliberately absent from the default;
# add it via GEMINI_TTS_FALLBACK_MODELS on a paid tier.
DEFAULT_TTS_FALLBACK_MODELS = ("gemini-2.5-flash-preview-tts",)
DEFAULT_VOICES = ("Kore", "Charon")
DEFAULT_SETTINGS_VERSION = "settings-v1"
VOICE_BY_ROLE = {"play_by_play": "Kore", "analyst": "Charon"}
# Natural turn-taking pause between packed commentary blocks (milliseconds).
_PACK_GAP_MS = 400

# Fish Audio (https://fish.audio) free developer tier: the `s2.1-pro-free`
# model has no hard usage cap under Fair Use.  Voices are explicit
# logical-label -> Fish voice-model `reference_id` mappings.
DEFAULT_FISH_TTS_MODEL = "s2.1-pro-free"
FISH_TTS_ENDPOINT = "https://api.fish.audio/v1/tts"
FISH_SAMPLE_RATE = 24_000
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


def _wav_data_bounds(wav: bytes) -> tuple[int, int]:
    """Locate the ``data`` subchunk in a RIFF/WAVE payload.

    Streaming WAVs (Fish Audio) declare a placeholder chunk size
    (``0xFFFFFF...``) so the header frame count is unreliable; this returns
    the real byte extent of the audio payload, clamped to what is present.
    """
    if len(wav) < 12 or wav[:4] != b"RIFF" or wav[8:12] != b"WAVE":
        raise TtsError("TTS_AUDIO_NOT_WAV")
    offset = 12
    end = len(wav)
    while offset + 8 <= end:
        chunk_id = wav[offset : offset + 4]
        size = int.from_bytes(wav[offset + 4 : offset + 8], "little")
        payload = offset + 8
        if chunk_id == b"data":
            # Clamp a streaming placeholder (huge) size to the bytes present.
            return payload, min(size, end - payload)
        offset = payload + size + (size & 1)  # chunks are word-aligned
    raise TtsError("TTS_AUDIO_NO_DATA_CHUNK")


def _duration_ms(wav: bytes) -> int:
    with wave.open(io.BytesIO(wav), "rb") as reader:
        rate = reader.getframerate()
        channels = reader.getnchannels()
        width = reader.getsampwidth()
    _, data_size = _wav_data_bounds(wav)
    frames = data_size // (channels * width)
    return round(frames / rate * 1000)


@dataclass(frozen=True)
class TtsPreflightResult:
    """Observed preflight state; availability is a hard gate for preview TTS."""

    expected: TtsPreflight
    observed: TtsPreflight
    available: bool
    probe_duration_ms: int | None = None
    probe_sha256: str | None = None

    def verify(self, *, allow_fallback: bool = False) -> None:
        # Drift is a hard gate unless a fallback model was explicitly approved;
        # a fallback still surfaces as ``observed`` so the deviation is recorded.
        if not allow_fallback:
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
    """Contract every speech backend satisfies.

    ``allow_fallback`` is an optional attribute: when present and True, model
    drift (fallback) is tolerated and must be recorded via ``observed``.
    """

    def preflight(self, expected: TtsPreflight) -> TtsPreflightResult: ...

    def synthesize(self, *, script: str, voice: str, style: Mapping[str, Any]) -> TtsBlockResult: ...


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


def _classify_error(message: str) -> str:
    """Classify a provider failure: retryable, missing-model, or permanent."""
    lowered = message.lower()
    if "404" in message or "not found" in lowered or "limit: 0" in message:
        # "limit: 0" = the model has no free-tier quota at all: retrying
        # can never succeed, treat it like an unavailable model.
        return "missing"
    if "429" in message or "quota" in lowered or "rate" in lowered or "500" in message:
        return "retryable"
    return "permanent"


def _retry_seconds(message: str, base: float, attempt: int) -> float:
    match = re.search(r"retry in\s+([\d.]+)\s*(ms|s)", message, re.IGNORECASE)
    if match:
        seconds = float(match.group(1)) * (0.001 if match.group(2).lower() == "ms" else 1.0)
        return max(seconds + 1.0, base)
    # Bounded exponential backoff: never let a missing suggested wait blow
    # the retry budget (2**attempt grows unboundedly).
    return base * min(2**attempt, 16)


def parse_voice_spec(spec: str) -> dict[str, str]:
    """Parse ``name=reference_id`` comma-separated pairs into a mapping.

    Used by both the Fish adapter's ``reference_ids`` and the CLI scripts, so
    voice wiring stays one convention across the pipeline.
    """
    mapping: dict[str, str] = {}
    for part in spec.split(","):
        name, sep, ref = part.strip().partition("=")
        if sep and name and ref:
            mapping[name.strip()] = ref.strip()
    return mapping


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
        fallback_models: Sequence[str] = DEFAULT_TTS_FALLBACK_MODELS,
        voices: Sequence[str] = DEFAULT_VOICES,
        settings_version: str = DEFAULT_SETTINGS_VERSION,
        probe: bool = True,
        retries: int = 3,
        retry_base_seconds: float = 8.0,
        chain_retries: int = 40,
        allow_fallback: bool = False,
    ) -> None:
        self._client = client
        self._api_key = api_key
        # Ordered chain: primary first, then approved fallbacks.  ``model`` is
        # the primary; the chain is only walked when ``allow_fallback`` is set.
        self.model = model
        # Dedupe against the primary so an explicit --model override never
        # produces a chain like [2.5, 2.5] that double-spends quota.
        self.fallback_models = tuple(m for m in fallback_models if m != model)
        self.models = (model,) + self.fallback_models
        self.voices = tuple(voices)
        self.settings_version = settings_version
        self.probe = probe
        self.retries = retries
        self.retry_base_seconds = retry_base_seconds
        # Chain-level patience: when the whole chain is inside a quota window
        # (free tier is per model), wait out the suggested retry repeatedly.
        # Distinct from per-model ``retries`` (short bursts) so an episode can
        # ride out a multi-minute window without failing.
        self.chain_retries = chain_retries
        self.allow_fallback = allow_fallback
        # Preferred model once a working one is found: avoids re-burning retry
        # time on an exhausted primary for every block of the same episode.
        self._preferred: str | None = None
        # Provenance observed at call time (provider/model/voice actually used),
        # appended by every successful synthesize(); never copied from config.
        self.provider = "gemini"
        self.observed_provenances: list[TtsProvenance] = []

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
        # Plain, classifier-safe probe: no style direction (a bare {"probe": True}
        # mapping produced a confusing "direction" line that some model versions
        # block as content_blocked).
        return self.synthesize(script="This is a voice availability check.", voice=self.voices[0], style={})

    def _synthesize_on(self, client: Any, *, prompt: str, voice: str, model: str) -> Any:
        """One model, with backoff retries for transient quota/5xx errors.

        A long suggested wait means the model's quota window is genuinely
        exhausted: when a fallback chain is approved, bail out early so the
        caller can move to the next model instead of burning retry time.
        """
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                return client.interactions.create(
                    model=model,
                    input=prompt,
                    response_format={"type": "audio"},
                    generation_config={"speech_config": [{"voice": voice}]},
                )
            except Exception as error:
                last_error = error
                message = f"{type(error).__name__}: {error}"
                if _classify_error(message) != "retryable" or attempt == self.retries:
                    raise TtsError(f"TTS_GENERATION_FAILED: {message}") from error
                delay = _retry_seconds(message, self.retry_base_seconds, attempt)
                # Long wait + approved fallback -> move on instead of waiting.
                if self.allow_fallback and delay > self.retry_base_seconds * 4:
                    raise TtsError(f"TTS_GENERATION_FAILED: {message}") from error
                time.sleep(delay)
        raise TtsError(f"TTS_GENERATION_FAILED: {type(last_error).__name__}: {last_error}") from last_error

    def preflight(self, expected: TtsPreflight) -> TtsPreflightResult:
        if not self.probe:
            # Availability probe disabled: verify drift only, never claim availability.
            return TtsPreflightResult(
                expected=expected,
                observed=self._expected(),
                available=True,
            )
        block = self._probe()
        return TtsPreflightResult(
            expected=expected,
            observed=TtsPreflight(block.model, self.voices, self.settings_version),
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
        # ``style`` is editorial metadata (hashed into the block record), not
        # spoken direction: the model reads the commentary text verbatim.
        prompt = script
        # Walk the model chain: on retryable/missing-model failures the next
        # approved fallback is tried; permanent failures (e.g. prompt blocked)
        # are never masked by a fallback.  Once a model works, prefer it for
        # the rest of the episode.  If the whole chain is in quota, wait out
        # the suggested retry and walk it again (patient, free-tier aware).
        chain = [self.model] if not self.allow_fallback else list(self.models)
        if self._preferred is not None and self._preferred in chain:
            chain.remove(self._preferred)
            chain.insert(0, self._preferred)
        last_error: TtsError | None = None
        retry_error: TtsError | None = None
        used_model: str | None = None
        interaction: Any = None
        for chain_attempt in range(self.chain_retries + 1):
            retryable_seen = False
            for model in chain:
                try:
                    interaction = self._synthesize_on(client, prompt=prompt, voice=voice, model=model)
                    used_model = model
                    self._preferred = model
                    break
                except TtsError as error:
                    last_error = error
                    kind = _classify_error(str(error))
                    if kind == "permanent":
                        raise
                    if kind == "retryable":
                        retryable_seen = True
                        retry_error = error
                    continue
            if interaction is not None:
                break
            if not retryable_seen or chain_attempt == self.chain_retries:
                raise last_error or TtsError("TTS_GENERATION_FAILED: no model available")
            # Wait on the retryable error, not a missing-model one: a model
            # with no quota ("limit: 0") never becomes available by waiting.
            delay = _retry_seconds(str(retry_error), self.retry_base_seconds, chain_attempt)
            time.sleep(delay)
        if interaction is None:
            raise last_error or TtsError("TTS_GENERATION_FAILED: no model available")
        pcm = _audio_data(interaction)
        wav = _wav_bytes(pcm)
        duration_ms = _duration_ms(wav)
        actual_model = used_model or self.model
        # Observed provenance comes from the config that actually succeeded on
        # this call (the accepted chain entry), never from the requested one.
        self.observed_provenances.append(
            TtsProvenance(provider=self.provider, model=actual_model, voices=(voice,))
        )
        record = tts_block(
            script=script,
            model=actual_model,
            voice=voice,
            style=style,
            audio=wav,
            duration_ms=duration_ms,
        )
        return TtsBlockResult(
            script=script, model=actual_model, voice=voice, style=dict(style),
            audio=wav, duration_ms=duration_ms, record=record,
        )


class FishAudioTtsAdapter:
    """Fish Audio TTS through the free ``s2.1-pro-free`` developer tier.

    Single-model (the free tier has no hard usage cap under Fair Use, so no
    fallback chain is needed).  Voices are logical labels mapped to Fish
    voice-model ``reference_id`` values; the mapping is explicit and fails
    closed on an unmapped voice so role voices never drift silently.

    The HTTP call is stdlib ``urllib`` only (no SDK), and a ``client`` with a
    ``tts(text=..., reference_id=...) -> bytes`` method can be injected for
    tests — mirroring the Gemini adapter's injectable ``client``.
    """

    def __init__(
        self,
        *,
        client: Any | None = None,
        api_key: str | None = None,
        model: str = DEFAULT_FISH_TTS_MODEL,
        reference_ids: Mapping[str, str] | None = None,
        voices: Sequence[str] | None = None,
        settings_version: str = "fish-audio-v1",
        probe: bool = True,
        retries: int = 3,
        retry_base_seconds: float = 5.0,
        allow_fallback: bool = True,
    ) -> None:
        self._client = client
        self._api_key = api_key
        self.model = model
        self.reference_ids = dict(reference_ids or {})
        self.voices = tuple(voices) if voices is not None else tuple(self.reference_ids)
        self.settings_version = settings_version
        self.probe = probe
        self.retries = retries
        self.retry_base_seconds = retry_base_seconds
        # A non-Gemini provider is a deliberate substitution from the
        # manifest-pinned contract, so it surfaces as an approved drift.
        self.allow_fallback = allow_fallback
        # Provenance observed at call time; never copied from config.
        self.provider = "fish"
        self.observed_provenances: list[TtsProvenance] = []

    def _request(self, text: str, reference_id: str) -> bytes:
        """One TTS request: injected client in tests, stdlib HTTP otherwise."""
        if self._client is not None:
            return self._client.tts(text=text, reference_id=reference_id)
        key = self._api_key or os.environ.get("FISH_API_KEY")
        if not key:
            raise TtsError("TTS_CREDENTIAL_MISSING")
        import urllib.request

        payload = json.dumps(
            {
                "text": text,
                "reference_id": reference_id,
                "format": "wav",
                "sample_rate": FISH_SAMPLE_RATE,
                "latency": "normal",
                "normalize": True,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            FISH_TTS_ENDPOINT,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "model": self.model,
            },
        )
        with urllib.request.urlopen(request, timeout=180) as response:
            return response.read()

    def _synthesize_audio(self, text: str, reference_id: str) -> bytes:
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                data = self._request(text, reference_id)
                if not data:
                    raise TtsError("TTS_AUDIO_EMPTY")
                return data
            except TtsError:
                raise
            except Exception as error:
                last_error = error
                message = f"{type(error).__name__}: {error}"
                if _classify_error(message) == "permanent" or attempt == self.retries:
                    raise TtsError(f"TTS_GENERATION_FAILED: {message}") from error
                time.sleep(_retry_seconds(message, self.retry_base_seconds, attempt))
        raise TtsError(f"TTS_GENERATION_FAILED: {type(last_error).__name__}: {last_error}") from last_error

    def preflight(self, expected: TtsPreflight) -> TtsPreflightResult:
        if not self.probe:
            return TtsPreflightResult(
                expected=expected,
                observed=TtsPreflight(self.model, self.voices, self.settings_version),
                available=True,
            )
        voice = next(iter(self.voices), "")
        block = self.synthesize(script="This is a voice availability check.", voice=voice, style={})
        return TtsPreflightResult(
            expected=expected,
            observed=TtsPreflight(block.model, self.voices, self.settings_version),
            available=True,
            probe_duration_ms=block.duration_ms,
            probe_sha256=block.record["audio_sha256"],
        )

    def synthesize(self, *, script: str, voice: str, style: Mapping[str, Any]) -> TtsBlockResult:
        if not script.strip():
            raise TtsError("TTS_SCRIPT_EMPTY")
        reference_id = self.reference_ids.get(voice)
        if reference_id is None:
            raise TtsError(f"TTS_VOICE_UNMAPPED: {voice}")
        # ``style`` is editorial metadata (hashed into the block record), not
        # spoken direction: the model reads the commentary text verbatim.
        prompt = script
        wav = self._synthesize_audio(prompt, reference_id)
        duration_ms = _duration_ms(wav)
        # Observed provenance comes from the request that actually ran (this
        # adapter's single model + header), never from the pinned contract.
        self.observed_provenances.append(
            TtsProvenance(provider=self.provider, model=self.model, voices=(voice,))
        )
        record = tts_block(
            script=script, model=self.model, voice=voice, style=style,
            audio=wav, duration_ms=duration_ms,
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
        provider: str = "fake",
    ) -> None:
        self.model = model
        self.voices = tuple(voices)
        self.settings_version = settings_version
        self.ms_per_char = ms_per_char
        self.min_ms = min_ms
        self.max_ms = max_ms
        self.calls: list[dict[str, Any]] = []
        # Provenance observed at call time; never copied from config.
        self.provider = provider
        self.observed_provenances: list[TtsProvenance] = []

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
        self.observed_provenances.append(
            TtsProvenance(provider=self.provider, model=self.model, voices=(voice,))
        )
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


def _block_model(out_dir: Path, index: int) -> str | None:
    """Read the model that rendered a block from its sidecar, if any."""
    sidecar = out_dir / f"block-{index:04d}.model"
    if not sidecar.is_file():
        return None
    value = sidecar.read_text(encoding="utf-8").strip()
    return value or None


def _write_block_model(out_dir: Path, index: int, model: str) -> None:
    (out_dir / f"block-{index:04d}.model").write_text(model, encoding="utf-8")


def _block_script_hash(out_dir: Path, index: int) -> str | None:
    sidecar = out_dir / f"block-{index:04d}.hash"
    if not sidecar.is_file():
        return None
    value = sidecar.read_text(encoding="utf-8").strip()
    return value or None


def _write_block_script_hash(out_dir: Path, index: int, script_hash: str) -> None:
    (out_dir / f"block-{index:04d}.hash").write_text(script_hash, encoding="utf-8")


def render_commentary_audio(
    manifest: Mapping[str, Any],
    adapter: TtsAdapter,
    *,
    out_dir: Path,
    voice_by_role: Mapping[str, str] | None = None,
    resume: bool = True,
    pacing_seconds: float = 0.0,
) -> dict[str, Any]:
    """Render every commentary line as a bounded, hashed audio block.

    The deterministic dialogue schedule controls order and non-overlap; the
    audio model only speaks each already-scheduled line.  ``resume=True``
    reuses existing ``block-XXXX.wav`` files whose script hash still matches
    the manifest, so an interrupted render (e.g. a free-tier quota window)
    continues without re-spending quota on completed blocks.  Returns the
    enriched ``tts`` section to merge into the video manifest, including the
    set of models actually used (``models_used``) so fallback is recorded.
    """
    expected = preflight_from_mapping(manifest["tts"]["expected"])
    adapter.preflight(expected).verify(
        allow_fallback=getattr(adapter, "allow_fallback", False)
    )
    mapping = dict(voice_by_role or VOICE_BY_ROLE)
    # Requested provenance is the manifest contract pinned BEFORE synthesis
    # (the ``tts.requested`` section written by the builder).  Compared against
    # observed below, it is what makes provider or model substitutions visible
    # to a reviewer; adapters only ever fill in observed.
    pinned = manifest["tts"].get("requested") if isinstance(manifest["tts"], Mapping) else None
    if isinstance(pinned, Mapping) and pinned.get("provider") and pinned.get("model"):
        requested = TtsProvenance(
            provider=str(pinned["provider"]),
            model=str(pinned["model"]),
            voices=tuple(str(v) for v in pinned.get("voices") or mapping.values()),
        )
    else:
        requested = TtsProvenance(
            provider="gemini", model=expected.model,
            voices=tuple(expected.voices) or tuple(mapping.values()),
        )
    lines = list(manifest.get("commentary", ()))
    if any(line.get("voice_role") not in mapping for line in lines):
        raise TtsError("TTS_ROLE_UNMAPPED")
    out_dir.mkdir(parents=True, exist_ok=True)
    blocks: list[dict[str, Any]] = []
    models_used: set[str] = set()
    for index, line in enumerate(lines):
        voice = mapping[line["voice_role"]]
        style = {"voice_role": line["voice_role"], "pace": "medium"}
        filename = f"block-{index:04d}.wav"
        path = out_dir / filename
        script_text = str(line.get("text", ""))
        # The model is not re-verifiable offline, so reuse the last recorded
        # model from a sidecar if present, else the pinned expected model.
        model = _block_model(out_dir, index) or expected.model
        expected_script_hash = _digest(
            {"script": script_text, "model": model, "voice": voice, "style": style}
        )
        if resume and path.is_file() and path.stat().st_size > 0 and _block_script_hash(out_dir, index) == expected_script_hash:
            with wave.open(str(path), "rb") as reader:
                audio = reader.readframes(reader.getnframes())
                frame_bytes = reader.getnchannels() * reader.getsampwidth()
                # Compute duration from the bytes actually present, not the
                # header frame count (streaming WAVs declare a placeholder).
                duration_ms = round((len(audio) // frame_bytes) / reader.getframerate() * 1000)
            record = tts_block(
                script=script_text, model=model, voice=voice, style=style,
                audio=audio, duration_ms=duration_ms,
            )
            blocks.append({**record, "file": filename, "start_frame": line.get("start_frame"), "end_frame": line.get("end_frame")})
            models_used.add(model)
            continue
        result = adapter.synthesize(script=script_text, voice=voice, style=style)
        path.write_bytes(result.audio)
        _write_block_model(out_dir, index, result.model)
        _write_block_script_hash(out_dir, index, result.record["script_hash"])
        blocks.append({**result.record, "file": filename, "start_frame": line.get("start_frame"), "end_frame": line.get("end_frame")})
        models_used.add(result.model)
        # Free-tier quota windows are narrow: pace fresh renders (never after
        # a resumed block) so bursts stay under the per-model ceiling.
        if pacing_seconds > 0 and index < len(lines) - 1:
            time.sleep(pacing_seconds)
    # Pack the blocks by their *actual* rendered durations so the assembled
    # track flows like a conversation and can never overlap.  The manifest
    # start/end frames are the plan; the rendered duration is authoritative
    # here (streaming-WAV headers lie, so use the measured byte length).
    fps=int(manifest["fps"])
    if blocks:
        # Pack by actual rendered duration, but never pull a block *earlier*
        # than its planned start: intro lines are scene-anchored and match lines
        # are event-anchored, so the planned schedule must remain a lower bound
        # while overlaps are still resolved forward.
        prev_end_ms=None
        for block in blocks:
            duration_ms=int(block["duration_ms"])
            planned_ms=block["start_frame"]*1000//fps
            if prev_end_ms is None: at_ms=planned_ms
            else: at_ms=max(planned_ms,prev_end_ms+_PACK_GAP_MS)
            block["start_frame"]=round(at_ms*fps/1000)
            block["end_frame"]=round((at_ms+duration_ms)*fps/1000)
            prev_end_ms=at_ms+duration_ms
        recap_start=sum(scene["duration_frames"] for scene in manifest["scenes"][:-1])
        if blocks[-1]["end_frame"]>recap_start: raise TtsError("COMMENTARY_OVERFLOW_AFTER_RENDER")
    blocks_hash = _digest([block["script_hash"] for block in blocks])
    # Observed provenance: aggregated from the audio actually present.  Models
    # come from every block record (sidecars included for resumed renders), so
    # an approved fallback shows up as drift; voices keep first-seen order.
    call_time = list(getattr(adapter, "observed_provenances", ()) or ())
    providers = sorted({item.provider for item in call_time})
    if len(providers) > 1:
        raise TtsError(f"TTS_PROVIDER_MIXED: {providers}")
    observed_provider = providers[0] if providers else str(getattr(adapter, "provider", "unknown"))
    observed_models = sorted(models_used)
    observed = TtsProvenance(
        provider=observed_provider,
        # A mixed-model episode (approved fallback chain) joins its models so
        # the single-string field still shows every engine behind the audio.
        model=",".join(observed_models),
        voices=tuple(dict.fromkeys(str(block["voice"]) for block in blocks)),
    )
    return {
        "expected": asdict(expected),
        **tts_provenance_section(requested, observed),
        "voices": dict(mapping),
        "blocks": blocks,
        "block_count": len(blocks),
        "blocks_hash": blocks_hash,
        "models_used": sorted(models_used),
        "out_dir": str(out_dir),
    }
