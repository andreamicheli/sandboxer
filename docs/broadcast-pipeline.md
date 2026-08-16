# Broadcast pipeline (TTS + YouTube) — runbook & provider contract

The narrated Series video flows through a fixed, deterministic pipeline. Model or
voice drift fails closed, every audio block is hashed, and the TTS provider
actually used is recorded in the broadcast record — never silent. This document
is the single source of truth for running the pipeline and for adding a new
speech backend.

## Stages

1. **Replay** — the frozen evidence bundle (`sandboxer.replay.v1`).
2. **Commentary draft** — a two-voice dialogue drafted by a `CommentaryDrafter`
   (or authored directly for a rehearsal), validated for grounding and typing,
   then human-reviewed. See "Commentary drafting" below.
3. **Manifest** — `build_video_manifest()` in `pilot/sandboxer_v0/video.py`
   freezes the editorial contract: the TTS expectation (`TtsPreflight`: model,
   voices, settings version) and the scheduled two-voice commentary, packed
   into a flowing dialogue with short natural pauses.
4. **TTS render** — `pilot/scripts/render_commentary_audio.py` synthesizes each
   commentary line through a provider adapter into bounded, hashed blocks and
   assembles a full-length `commentary-full.wav`.
5. **Video render** — Remotion (`video/src/index.tsx`) renders `video-only.mp4`
   from `remotion-props.json` (`{"manifest": <video-manifest.json>}`).
6. **Mux** — `ffmpeg_delivery_commands()` in `video.py` (probe → loudness
   normalize → mux → delivery encode) produces `delivery.mp4`.
7. **Publish** — `pilot/scripts/publish_broadcast.py` uploads to YouTube
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

## Commentary drafting

Commentary is never terminal text read aloud: it is a drafted, two-voice
narrative. `pilot/sandboxer_v0/commentary.py` owns that step:

- `CommentaryDrafter` protocol — `draft(replay, report) -> list[lines]`.
- `GeminiCommentaryDrafter` — the production mechanism: builds a grounded
  prompt from the replay frames + report outcome and calls a Gemini text model
  (injectable `client`; control-plane `GEMINI_API_KEY` otherwise).
- `validate_commentary(lines, frames)` — every line must be grounded to real
  `event_ids`, use a known `voice_role` (`play_by_play` | `analyst`), carry a
  known `line_type` (`observed` | `interpreted` | `editorial`), and interpreted
  lines must carry a hedge marker (`appears`, `seems`, …).

`build_video_manifest(..., commentary=<lines>)` schedules a validated draft
with a word-based reading-time budget and raises `COMMENTARY_OVERFLOW` if the
plan would run past the Match into the recap. Without a draft it falls back to
reading the terminal events verbatim (a rehearsal placeholder only). Human
review of the draft is mandatory before publication.

Overlap is prevented at render time, not just planned for: after TTS renders
the blocks, `render_commentary_audio()` re-packs them by their *actual*
audio durations (400ms turn gap) and writes that final schedule back into the
manifest, so the assembled track, captions, and on-screen caption bar stay in
sync and can never overlap. Resume also verifies each block's script hash
sidecar, so a changed line re-renders instead of silently reusing stale audio.

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

# 2. (Rehearsal) rebuild replay/report/manifest incl. drafted commentary
uv run python scripts/build_synthetic_artifacts.py

# 3. TTS commentary -> artifacts/commentary-full.wav (resumable).  Packs the
#    blocks by their actual rendered durations (no overlap, natural turn gaps)
#    and writes that final schedule back into video-manifest.json.
uv run python scripts/render_commentary_audio.py --provider fish --no-resume

# 4. Regenerate renderer props + captions from the packed schedule
python3 -c "import json; json.dump({'manifest': json.load(open('artifacts/video-manifest.json'))}, open('artifacts/remotion-props.json','w'), indent=2)"
uv run python scripts/build_captions.py

# 5. Video -> artifacts/video-only.mp4 (Remotion)
cd ../video && npx remotion render src/index.tsx SandboxerSeries ../artifacts/video-only.mp4 --props=../artifacts/remotion-props.json && cd ../pilot

# 6. Mux -> artifacts/delivery.mp4 (probe -> loudnorm -> mux -> delivery)
ffmpeg -y -nostdin -i artifacts/commentary-full.wav -af "loudnorm=I=-16:LRA=7:TP=-1.5" -c:a pcm_s24le artifacts/commentary-full.normalized.wav
ffmpeg -y -nostdin -i artifacts/video-only.mp4 -i artifacts/commentary-full.normalized.wav -map 0:v:0 -map 1:a:0 -c:v copy -c:a aac -b:a 320k artifacts/master.mov
ffmpeg -y -nostdin -i artifacts/master.mov -c:v libx264 -crf 18 -pix_fmt yuv420p -c:a aac -movflags +faststart artifacts/delivery.mp4

# 4. Publish (unlisted) + captions + thumbnail.  Unattended by default;
#    pass --manual to re-enable the human gate (--approved-by + confirmation).
#    With a frozen evidence bundle add:
#    --bundle evidence.json --site-base-url https://<site>
#    to also stage the canonical + detailed + LLM-narrative reports on the site.
uv run python scripts/publish_broadcast.py \
  --manifest artifacts/video-manifest.json --report artifacts/report.json \
  --video artifacts/delivery.mp4 --captions artifacts/captions.vtt \
  --thumb artifacts/thumbnail.png --audio-dir artifacts/broadcast.audio \
  --out artifacts/broadcast.json --tts fish --youtube real --privacy unlisted
```

Synthetic artifacts (replay/report/manifest) for a rehearsal come from
`pilot/scripts/build_synthetic_artifacts.py`; captions from
`pilot/scripts/build_captions.py`.

## Downloadable LaTeX report

Every Series Result Report also ships a self-contained LaTeX document with the
detailed per-Match analysis (per-competitor, per-phase token / turn / tool
accounting) and matplotlib data charts (output tokens, tool calls, turns, and a
phase timeline) — compiled to `report-detailed.pdf`, with `report-detailed.tex`
as the source. It is produced by `pilot/sandboxer_v0/report_latex.py` and
`pilot/scripts/build_report_pdf.py`, and `build_release()` embeds the LaTeX
source plus the compiled-PDF hash (`compile_latex=True` to compile) and links
both from the published site.

On top of that deterministic document, `pilot/sandboxer_v0/report_narrative.py`
drafts an **LLM-written narrative** (summary, one short narrative per Match,
analysis, limitations) grounded to the deterministic report model, with a
`DeterministicReportNarrativeDrafter` template fallback and fail-closed
validation. `publish_broadcast.py --bundle ...` stages it as
`narrative.tex`/`narrative.pdf` (LaTeX) plus `narrative.html` — the less
detailed site subpage that the video description links to — while the canonical
`report.html` and `report-detailed.pdf` remain the authoritative facts.

```sh
# From a frozen evidence bundle JSON (or omit --bundle for a demo fixture):
uv run python scripts/build_report_pdf.py --bundle evidence.json \
  --out-dir artifacts/report-pdf
```

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
