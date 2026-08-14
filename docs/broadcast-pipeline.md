# Broadcast pipeline (TTS + YouTube) — runbook & provider contract

The narrated Series video flows through a fixed, deterministic pipeline. Model or
voice drift fails closed, every audio block is hashed, and the TTS provider
actually used is recorded in the broadcast record — never silent. This document
is the single source of truth for running the pipeline and for adding a new
speech backend.

## Stages

1. **Replay** — the frozen evidence bundle (`sandboxer.replay.v1`).
2. **Manifest** — `build_video_manifest()` in `pilot/sandboxer_v0/video.py`
   freezes the editorial contract: the TTS expectation (`TtsPreflight`: model,
   voices, settings version) and the deterministic commentary schedule.
3. **TTS render** — `pilot/scripts/render_commentary_audio.py` synthesizes each
   commentary line through a provider adapter into bounded, hashed blocks and
   assembles a full-length `commentary-full.wav`.
4. **Mux** — `ffmpeg_delivery_commands()` in `video.py` (probe → loudness
   normalize → mux → delivery encode) produces `delivery.mp4`.
5. **Publish** — `pilot/scripts/publish_broadcast.py` uploads to YouTube
   (`unlisted` by default), sets captions + thumbnail, and writes
   `broadcast.json` (the provenance record).

## The TtsAdapter contract

Any speech backend implements the `TtsAdapter` protocol in
`pilot/sandboxer_v0/tts.py`:

- `preflight(expected: TtsPreflight) -> TtsPreflightResult` — probe the live
  endpoint once and verify availability. Model/voice drift raises
  `TTS_PREFLIGHT_DRIFT` unless explicitly approved (recorded, never silent).
- `synthesize(*, script, voice, style) -> TtsBlockResult` — one line → one
  bounded, hashed WAV block (16-bit mono, 24 kHz).

`render_commentary_audio()` drives the schedule and is resumable: it reuses
existing `block-XXXX.wav` files whose script hash still matches, so an
interrupted render never re-spends quota. It also returns `models_used`.

Reference implementations (both live in `tts.py`):

- `GeminiTtsAdapter` — SDK-based, per-model fallback chain for exhausted
  free-tier quota.
- `FishAudioTtsAdapter` — stdlib `urllib` only, single model, the simplest to
  copy for a new REST-only provider.
- `FakeTtsAdapter` — deterministic, credential-free (tests + dry runs).

## Adding a new TTS provider

Copy `FishAudioTtsAdapter` and adapt. The steps:

1. **Implement the adapter** (in `tts.py` or a sibling module):
   - injectable `client` so tests never touch the network,
   - retry using the provider's own suggested wait; classify 429/5xx as
     retryable and everything else as permanent (never masked),
   - `_duration_ms` must read the actual payload bytes, not the WAV header
     frame count — streaming WAVs (Fish) declare a placeholder size.
2. **Expose it** — add a `--provider` branch in
   `render_commentary_audio.py` and a `--tts` branch in `publish_broadcast.py`.
3. **Configure it** — add env vars to `pilot/.env.example` and a
   `setup_credentials.py` subcommand (writes `.env`, chmod 600, masked output).
4. **Test it** — add cases to `pilot/tests/test_tts.py` (synthesize, retry,
   voice mapping, resume, drift) and run `uv run pytest`.

## Config surface

| Concern | Env vars |
|---|---|
| Gemini | `GEMINI_API_KEY`, `GEMINI_TTS_FALLBACK_MODELS`, `SANDBOXER_TTS_ALLOW_FALLBACK` |
| Fish | `FISH_API_KEY`, `FISH_TTS_VOICES=Kore=<ref>,Charon=<ref>`, `FISH_TTS_MODEL` |
| Default provider | `SANDBOXER_TTS_PROVIDER=gemini|fish` |
| YouTube | `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`, `YOUTUBE_REFRESH_TOKEN` |

## End-to-end runbook

```sh
cd pilot && set -a && . ./.env && set +a

# 1. Credentials (control plane only)
uv run python scripts/setup_credentials.py fish --probe       # or: gemini --key AIza... --probe
uv run python scripts/setup_credentials.py youtube --flow manual --client-id <id> --client-secret <secret>

# 2. TTS commentary -> artifacts/commentary-full.wav (resumable)
uv run python scripts/render_commentary_audio.py --provider fish

# 3. Mux -> artifacts/delivery.mp4 (probe -> loudnorm -> mux -> delivery)
ffmpeg -y -nostdin -i artifacts/commentary-full.wav -af "loudnorm=I=-16:LRA=7:TP=-1.5" -c:a pcm_s24le artifacts/commentary-full.normalized.wav
ffmpeg -y -nostdin -i artifacts/video-only.mp4 -i artifacts/commentary-full.normalized.wav -map 0:v:0 -map 1:a:0 -c:v copy -c:a aac -b:a 320k artifacts/master.mov
ffmpeg -y -nostdin -i artifacts/master.mov -c:v libx264 -crf 18 -pix_fmt yuv420p -c:a aac -movflags +faststart artifacts/delivery.mp4

# 4. Publish (unlisted) + captions + thumbnail
uv run python scripts/publish_broadcast.py \
  --manifest artifacts/video-manifest.json --report artifacts/report.json \
  --video artifacts/delivery.mp4 --captions artifacts/captions.vtt \
  --thumb artifacts/thumbnail.png --audio-dir artifacts/broadcast.audio \
  --out artifacts/broadcast.json --tts fish --youtube real --privacy unlisted \
  --approved-by editor --yes
```

Synthetic artifacts (replay/report/manifest) for a rehearsal come from
`pilot/scripts/build_synthetic_artifacts.py`; captions from
`pilot/scripts/build_captions.py`.

## Gotchas (each is encoded in a test)

- **Streaming WAV** (Fish Audio) — the RIFF/data sizes are placeholders; read
  the real byte length, not `wave.getnframes()`.
- **`captions.insert` requires `snippet.name`** — empty string is valid;
  omitting it is a 400 `invalidMetadata`.
- **Custom thumbnails need a phone-verified YouTube channel** — an unverified
  channel returns 403; the thumbnail step is non-fatal and recorded.
- **Gemini's classifier rejects offensive-security wording** (e.g. "SQLi
  payload") — use neutral fixture text; a 400 is permanent, never retried.
- **Free-tier quotas are per model** — Gemini uses an approved fallback chain;
  Fish's `s2.1-pro-free` has no hard cap under Fair Use (verify commercial-use
  terms before public monetized publishing).
