---
name: add-tts-provider
description: Integrate a new text-to-speech backend into the Sandboxer broadcast pipeline. Use when a new TTS provider (Speechify, ElevenLabs, Piper, Coqui, ...) must replace or augment Gemini/Fish for commentary narration.
---

# Add a TTS provider to the Sandboxer broadcast pipeline

Goal: a new speech backend renders the two-role commentary (play-by-play +
analyst) without touching the deterministic schedule, hashing, resume, or mux
machinery.

## Contract to satisfy

Implement the `TtsAdapter` protocol from `pilot/sandboxer_v0/tts.py`:

- `preflight(expected: TtsPreflight) -> TtsPreflightResult`
- `synthesize(*, script: str, voice: str, style: Mapping) -> TtsBlockResult`

The returned `TtsBlockResult` must carry `audio` (a 16-bit mono 24 kHz WAV
container), `duration_ms`, `model`, `voice`, and a `record` produced by
`tts_block()` (which hashes script+model+voice+style and the audio).

## Procedure

1. **Copy the reference** — `FishAudioTtsAdapter` (stdlib `urllib`, single
   model) is the simplest template; `GeminiTtsAdapter` shows a fallback chain.
   Keep an injectable `client` so tests never hit the network.
2. **Retry & classify** — reuse `_classify_error` / `_retry_seconds`; 429/5xx
   retryable, other 4xx permanent (never masked by a fallback).
3. **Duration** — compute `duration_ms` from actual payload bytes via
   `_duration_ms` (streaming WAVs declare placeholder sizes).
4. **Wire it** — add a `--provider` branch in
   `pilot/scripts/render_commentary_audio.py`, a `--tts` branch in
   `pilot/scripts/publish_broadcast.py`, and a `setup_credentials.py`
   subcommand (writes `.env`, chmod 600).
5. **Configure** — document env vars in `pilot/.env.example`.
6. **Test** — add cases to `pilot/tests/test_tts.py`: synthesize, retry, voice
   mapping fails closed, resume, and drift approval. Run `uv run pytest`.

## Verify end to end

```sh
cd pilot && set -a && . ./.env && set +a
uv run python scripts/render_commentary_audio.py --provider <name> --no-resume
```

Then mux and publish per `docs/broadcast-pipeline.md`.

## Acceptance

- `uv run pytest` is green (all 368+ tests).
- A real render produces 9+ hashed blocks and a `commentary-full.wav`.
- The broadcast record's `tts.models_used` names the new provider.
