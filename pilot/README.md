# Local pilot

This directory contains the disposable, local content-viability baseline for
Sandboxer. It is the first real-model implementation from which subsequent
iterations are developed.

The current pilot compares two models under Command Code, the canonical initial
provider for both Competitors; see
[`../docs/provider-map.md`](../docs/provider-map.md). The first controlled pair is
Laguna (`poolside/laguna-s-2.1-free`) versus Muse Spark Contributor
(`meta/muse-spark-1.2-contributor`). Provider calls are turn-based to respect
pilot-tier rate limits; the runners remain isolated and cannot observe one
another during the blue phase.

The earlier Inspect/Groq path (`live_match.py`, macOS/Colima era) is retained
only as historical reference; the Inspect *harness* itself is back as the
canonical way to run a Match (see below), pointed at Command Code instead of
Groq.

The default configuration is a rate-limited pilot: 1,536/2,048 output tokens
per blue/red phase and model, a 512-token per-turn ceiling, three/four turns,
and a 25-second minimum gap between provider calls.

The Inspect score represents successful, complete orchestration (a Match that
produced a `VALID_CAPTURE` or `VALID_NO_CAPTURE` outcome), not model quality.
Per-agent errors, availability, captures, token usage, and timestamps remain
in the JSONL artifact for interpretation and video production.

## Safety invariants

- Compose may use named volumes, never bind mounts.
- Agent containers are non-privileged, read-only at the root filesystem,
  capability-free, PID/memory/CPU bounded, and disconnected from a direct
  egress network.
- During blue phase the runners have no shared network. The orchestrator creates
  and attaches an internal arena network only at the red-phase transition.
- Neither runner has external network access.
- The Groq key (legacy Inspect path) exists only in the host-side `.env`, never in an image or volume.
- The preflight fails closed before any agent process is started.
- Credentials never enter the Inspect eval logs: the solver stores only the
  redaction-safe result payload in the task state.

## Stages

```sh
uv sync
uv run python scripts/preflight.py
docker compose build
docker compose up -d
uv run pytest
uv run python scripts/dry_run.py
```

The deterministic dry-run uses no model, login, API key, or provider call.
Provider validation and the live match are intentionally separate stages.

The canonical way to run one private calibration Match is the Inspect task
(see next section); `scripts/run_command_code_match.py` remains as the thin
direct CLI around the same engine:

```sh
uv run python scripts/preflight_command_code.py      # deterministic provider preflight
sudo -E uv run inspect eval inspect_match.py \
  -T match_id=<unique-match-id> \
  -T image=/var/lib/sandboxer/images/sandboxer-runner-v0.qcow2 \
  -T profile=/var/lib/sandboxer/profiles/sandboxer-runner.json \
  --display plain                                   # one private calibration Match
```

### The Inspect Match task

`inspect_match.py` exposes `sandboxer_match`, an Inspect task that wraps the
exact engine behind `run_command_code_match.py` (no logic is duplicated: the
solver awaits `scripts.run_command_code_match.execute_match`), so the evidence
bundle, the telemetry JSONL and every safety invariant are byte-for-byte
identical to the CLI path.

- **No model API through Inspect.**  The task never calls `generate`; it uses
  Inspect's local `mockllm` provider only to satisfy the task runtime (same
  pattern as `inspect_task.py`).  Provider calls belong to the Command Code
  CLI, which is never copied into a Runner.
- **Task args.**  Required: `match_id`, `image`, `profile`.  Optional: `models`
  (defaults to the canonical first pair), `seed`, `runner_root`,
  `evidence_dir`, `ttl_seconds`, `phase_timeout`, `blue_tokens`, `red_tokens`,
  `interview_tokens`, `blue_turns`, `red_turns`, `blue_tools`, `red_tools`
  (same defaults as the CLI).  Per-tool-call control (phase tool allowlist,
  tool ceiling, native-tool rejection) stays in `RunnerToolServer` and the
  frame monitor — Inspect never sees the tool calls.
- **Scorer.**  Maps the engine outcome taxonomy onto a score: `VALID_CAPTURE`
  and `VALID_NO_CAPTURE` score `C` (complete); anything else (missing payload,
  non-`passed` result) scores `I`.  `BUDGET_EXHAUSTED` / `INVALID` paths
  surface as non-passing samples or sample errors, and the JSONL records the
  safe reason code.  Metadata is redaction-safe: no flags, tokens or
  credentials ever enter the eval log.
- **No retries of the same Match ID.**  The evidence bundle and telemetry file
  are created with `O_EXCL`, so re-running a `match_id` fails fast — a failed
  Match is a datum, not something to hide.
- **Logs.**  Each run writes an eval log under `logs/`, browsable with
  `inspect view`.  Task discovery: `inspect list tasks "inspect_match.py"`.

## Model-connection gate

The deterministic dry-run and the provider preflight use no model, login, API
key, or provider call. Real provider calls require the coherent two-part gate
`model_mode: command_code` and `allow_provider_calls: true`. Authentication
remains owned by the installed Command Code CLI and is never copied into a
Runner.

## Broadcast handoff (TTS + YouTube)

The narrated Series video is derived from the frozen evidence bundle: the
editorial manifest (`sandboxer_v0/video.py`) declares the TTS contract, then
`sandboxer_v0/tts.py` renders the two-voice commentary as bounded, hashed audio
blocks through a replaceable TTS adapter (Gemini
`gemini-3.1-flash-tts-preview` with voices Kore/Charon by default; Fish Audio
`s2.1-pro-free` as a free alternative), and `sandboxer_v0/youtube.py` performs
the publication handoff to the YouTube Data API v3.

### TTS adapter architecture

`sandboxer_v0/tts.py` exposes a small `TtsAdapter` protocol (preflight +
synthesize) so any backend can drive the deterministic commentary schedule.
`GeminiTtsAdapter` is the real implementation; `FakeTtsAdapter` is
deterministic and credential-free.  Key behaviors, all covered by tests:

- **Contract drift fails closed.**  The manifest pins model + voices + settings
  (`TtsPreflight`); preflight probes the live endpoint and any mismatch raises
  `TTS_PREFLIGHT_DRIFT` — no silent sound changes.
- **Per-model retry with Google's suggested wait.**  A 429 body says exactly
  when to retry; the adapter honors it (never a blind fixed backoff).
- **Approved fallback chain.**  Free-tier quota is enforced per model, so when
  the primary's window is exhausted the adapter walks an approved chain
  (`DEFAULT_TTS_FALLBACK_MODELS` = Gemini 2.5 Flash TTS preview; configurable
  via `GEMINI_TTS_FALLBACK_MODELS`).  Fallback is only active when
  `SANDBOXER_TTS_ALLOW_FALLBACK=1` is set, permanent errors (e.g. a blocked
  prompt) are never masked, the last working model is preferred for the rest
  of the episode, and the deviation is **recorded** (`models_used` per block
  and in the broadcast record) — never silent.  2.5 Pro preview is excluded by
  default because it has no free-tier quota (`limit: 0`).
- **Resumable rendering.**  `render_commentary_audio(resume=True)` reuses
  existing `block-XXXX.wav` files whose script hash still matches the manifest,
  so an interrupted render (quota window, session drop) continues without
  re-spending quota on completed blocks.
- **Classifier-safe prompts.**  Commentary text must avoid offensive-security
  vocabulary: Google's prompt classifier rejects inputs like "SQLi payload"
  with `content_blocked` (HTTP 400).  The synthetic fixtures use neutral
  wording; a 400 is treated as permanent and never retried or masked.
- **Fish Audio adapter (`s2.1-pro-free`).**  `FishAudioTtsAdapter` hits the
  single `POST /v1/tts` endpoint with stdlib `urllib` (no SDK) and maps
  logical role voices to Fish voice-model `reference_id`s via
  `FISH_TTS_VOICES=Kore=<ref>,Charon=<ref>`.  The free developer tier has no
  hard usage cap under Fair Use, so there is no fallback chain; transient
  429/5xx are retried with backoff and an unmapped voice fails closed.

The canonical render entry point is `scripts/render_commentary_audio.py`,
which synthesizes every commentary line and assembles a full-length 24 kHz
mono track (`artifacts/commentary-full.wav`) aligned to each block's
`start_frame` for muxing with the Remotion video:

```sh
SANDBOXER_TTS_ALLOW_FALLBACK=1 uv run python scripts/render_commentary_audio.py
```

Fish Audio (free tier) is selected with `--provider fish`:

```sh
FISH_API_KEY=... FISH_TTS_VOICES="Kore=<ref>,Charon=<ref>" \
  uv run python scripts/render_commentary_audio.py --provider fish
```

It is resumable by default (`--no-resume` to force a full re-render) and paces
fresh renders to stay under the free-tier ceiling.  `publish_broadcast.py`
accepts `--tts fish` the same way.

### Credentials setup (control plane only)

Fill the broadcast env vars with `scripts/setup_credentials.py` (writes
`pilot/.env`, chmod 600):

- **Gemini TTS key** — create one at https://aistudio.google.com/apikey, then:
  `uv run python scripts/setup_credentials.py gemini --key AIza... --probe`
  (`--probe` runs one tiny real synthesis to prove the key and the pinned
  preview model work).
- **YouTube OAuth login** — in Google Cloud Console: enable **YouTube Data API
  v3**, configure the **OAuth consent screen** (add your Google account as a
  test user), create an **OAuth client ID** of type **Desktop app**, then run:
  `uv run python scripts/setup_credentials.py youtube --client-id <id> --client-secret <secret>`
  The script stores the refresh token and verifies the channel name.  On a
  headless host there is no browser, use `--flow manual` and paste back the
  redirect URL from any device's browser (works from a phone, no tunnel):
  `uv run python scripts/setup_credentials.py youtube --flow manual --client-id <id> --client-secret <secret>`
  Alternatively `--flow local` (default) starts a loopback server on
  `--port 8080`; tunnel it from a machine that has a browser:
  `ssh -L 8080:localhost:8080 <user>@<this-host>`

Credential-free rehearsal (deterministic fake TTS, fake YouTube service):

```sh
uv run python scripts/publish_broadcast.py \
  --manifest artifacts/video-manifest.json \
  --video artifacts/delivery.mp4 \
  --tts fake --youtube fake --out artifacts/broadcast.json
```

Real uploads default to `unlisted`, require `GEMINI_API_KEY` plus the
`YOUTUBE_CLIENT_ID`/`YOUTUBE_CLIENT_SECRET`/`YOUTUBE_REFRESH_TOKEN` OAuth
refresh-token env vars, and are unattended by default (human gates off). Pass
`--manual` to re-enable the human gate (`--approved-by <reviewer>` plus an
interactive confirmation):

```sh
uv run python scripts/publish_broadcast.py \
  --manifest artifacts/video-manifest.json \
  --report artifacts/report.json \
  --video artifacts/delivery.mp4 --captions artifacts/captions.vtt \
  --thumb artifacts/thumbnail.png \
  --tts fish --youtube real --privacy unlisted
```

Credentials live only in the control-plane `.env` and are never copied into a
Runner. The recorded handoff (video id, privacy status, TTS block hash, review
draft captions) can be passed to `build_release(..., broadcast=...)` so the
release site links to the episode.
