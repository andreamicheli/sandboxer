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
