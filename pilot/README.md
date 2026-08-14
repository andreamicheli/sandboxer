# Local pilot

This directory contains the disposable, local content-viability baseline for
Sandboxer. It is the first real-model implementation from which subsequent
iterations are developed.

The pilot runs in a dedicated Colima VM with host-directory mounts disabled.
Inside that VM, two bounded containers begin on distinct private networks.
Inspect calls Groq from the host orchestrator; model credentials and provider
network access are never passed into either runner.

The current pilot compares two Groq-hosted models under the same Inspect
harness. The canonical initial provider is Command Code for both Competitors; see
[`../docs/provider-map.md`](../docs/provider-map.md). Command Code requires a
headless CLI adapter before it can replace the current Groq path.
Provider calls are turn-based to respect pilot-tier rate limits; the runners
remain isolated and cannot observe one another during the blue phase.

The default configuration is a rate-limited pilot: 1,536/2,048 output tokens
per blue/red phase and model, a 512-token per-turn ceiling, three/four turns,
and a 25-second minimum gap between provider calls.

Inspect's score represents successful orchestration, not model quality. Per-agent
errors, availability, captures, token usage, and timestamps remain in the JSONL
artifact for interpretation and video production.

## Safety invariants

- The macOS account must not be an administrator.
- Colima must start with host mounts disabled (`--mount none`).
- Compose may use named volumes, never bind mounts.
- Agent containers are non-privileged, read-only at the root filesystem,
  capability-free, PID/memory/CPU bounded, and disconnected from a direct
  egress network.
- During blue phase the runners have no shared network. The orchestrator creates
  and attaches an internal arena network only at the red-phase transition.
- Neither runner has external network access.
- The Groq key exists only in the host-side `.env`, never in an image or volume.
- The preflight fails closed before any agent process is started.

## Stages

```sh
./scripts/runtime.sh start
uv sync
uv run python scripts/preflight.py
docker compose build
docker compose up -d
uv run pytest
uv run python scripts/dry_run.py
uv run inspect eval inspect_task.py --model mockllm/model --display plain

# After explicit Groq approval and populating .env
set -a && . ./.env && set +a
uv run inspect eval live_match.py --model mockllm/model --display plain \
  --log-dir artifacts/inspect-live --max-retries 0 --timeout 60
```

Provider validation and the live match are intentionally separate stages. Never
mount a macOS home directory.

## Model-connection gate

The deterministic dry-run uses no model, login, API key, or provider call.
The Inspect smoke task uses its built-in local `mockllm` runtime but never
calls `generate`; it wraps the same deterministic orchestration test.
Real provider calls require the coherent two-part gate `model_mode: command_code`
and `allow_provider_calls: true`. Authentication remains owned by the installed
Command Code CLI and is never copied into a Runner.

## Broadcast handoff (TTS + YouTube)

The narrated Series video is derived from the frozen evidence bundle: the
editorial manifest (`sandboxer_v0/video.py`) declares the TTS contract, then
`sandboxer_v0/tts.py` renders the two-voice commentary as bounded, hashed audio
blocks through the replaceable Gemini TTS adapter
(`gemini-3.1-flash-tts-preview`, voices Kore/Charon), and
`sandboxer_v0/youtube.py` performs the publication handoff to the YouTube
Data API v3.

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
refresh-token env vars, and demand explicit human approval
(`--approved-by <reviewer>`):

```sh
uv run python scripts/publish_broadcast.py \
  --manifest artifacts/video-manifest.json \
  --report artifacts/report.json \
  --video artifacts/delivery.mp4 --captions artifacts/captions.vtt \
  --thumb artifacts/thumbnail.png \
  --tts real --youtube real --privacy unlisted --approved-by editor --yes
```

Credentials live only in the control-plane `.env` and are never copied into a
Runner. The recorded handoff (video id, privacy status, TTS block hash, review
draft captions) can be passed to `build_release(..., broadcast=...)` so the
release site links to the episode.
