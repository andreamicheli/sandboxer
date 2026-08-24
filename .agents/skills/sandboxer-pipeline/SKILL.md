---
name: sandboxer-pipeline
description: "Use when working on SandBoxer tasks or its pipeline."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [sandboxer, ctf, pipeline, youtube, remotion, tts]
---

# SandBoxer Pipeline

Class-level guide for Andrea's SandBoxer project (`~/projects/SandBoxer`, GitHub
`andreamicheli/sandboxer`): model-vs-model CTF runs rendered into broadcast
videos with reports published to Vercel + YouTube unlisted.

## Publication policy (user-mandated, hard rules)

1. Every run uploads to YouTube **unlisted automatically** — no approval needed.
   The author reviews video + report together.
2. Flipping to PUBLIC requires an explicit approval token
   (`SANDBOXER_PUBLISH_APPROVAL` env or CLI flag) via `publish_public()`.
3. Upload-time `privacy_status=public` is refused outright — public is only
   ever a later status flip.
4. Hard gate: no upload unless the evidence bundle exists, the run-specific
   report URL responds HTTP 200 (never a placeholder or bare repo URL), and
   `publications.json` has the slug indexed. Enforced by
   `pilot/sandboxer_v0/publish_gate.py`; bypass only via explicit
   `allow_ungated=True`.
5. TTS default is **Fish Audio** (`s2.1-pro-free`, Kore play-by-play / Charon
   analyst). Gemini remains available only via explicit override.

## Module map (pilot/sandboxer_v0/, all covered by pytest)

| Concern | Module |
|---|---|
| Publish gate | `publish_gate.py` |
| Persistent jobs | `job_runner.py` (setsid detach, PID files, killpg stop, dup-ID rejection) |
| Run isolation | `run_layout.py` → `artifacts/runs/<run_id>/` + sha256 manifest |
| Telemetry→replay | `artifact_converter.py` (canonical evidence/replay schemas) |
| Scheduler authority | `schedule.py validate_and_pack` — deterministic clamp/repack/drop beats any LLM offsets |
| TTS provenance | requested vs observed in manifests + `tts_provider_drift` flag |
| Render preflight | `render_preflight.py` — concurrency from RAM, tmpfs→persistent TMPDIR |

## Conventions

- Commit style: granular per-concern commits,
  `feat(pilot)/fix(publish)/feat(tts): <summary>` + body explaining the
  incident-class problem solved.
- Every fix ships with its own pytest file; full suite before declaring done.
- Known pre-existing failure: `test_setup_credentials.py::test_orca_rejects_missing_key`
  fails on clean HEAD (env-dependent, orca key present locally). Not ours to fix.
- Editorial principle: LLM proposes text/intent; deterministic scheduler owns
  timing, budgets, and factual outcome text (from `outcome_basis`).
| Incidenti e fix | `references/pipeline-fixes.md` |
| Stato pipeline corrente | `references/pipeline-state-v7.md` |
| Render Remotion con RAM limitata | `references/video-render-ram.md` |
| Agent-match frame-monitor debugging | `references/agent-match-failures.md` |
- Agent-match frame-monitor debugging reference (native-tool semantics, v1–v4
  failure autopsies, correct retry loop): see `references/agent-match-failures.md`.

## Pitfalls (session-verified; encode so future runs don't re-learn)

### Remotion `--log` accepts a level, not a file path

`npx remotion render ... --log=render.log` fails with
`Invalid --log value passed. Accepted values: 'trace', 'verbose', 'info', 'warn', 'error'`.

Correct: `--log=error` (or omit; default is `error`). Capture output by redirecting
stderr: `npx remotion render ... --log=error 2>render.log`.

### `custom-intro.mp4` must be in `video/assets/`, not `video/public/assets/`

`CustomIntroScene` calls `staticFile('assets/custom-intro.mp4')`, which resolves
relative to the Remotion project root (`video/`). The file ships under
`video/public/assets/custom-intro.mp4` (web access) but Remotion does not look in
`public/`. Before rendering, ensure:
```
video/assets/custom-intro.mp4  (copy from video/public/assets/ if missing)
```
Missing this causes a timeout at the frame inside the `custom_intro` scene (e.g.
frame 84 of 240) with:
`TimeoutError: waiting for the page to render the React component at frame 84 failed: timeout 33000ms exceeded`

### TTS render scripts lose env vars under `sudo`

`render_commentary_audio.py` reads `FISH_API_KEY`, `FISH_TTS_VOICES`, etc. from the
process environment. `sudo` strips env, so even with `.env` present the script fails
with `FISH_KEY_MISSING: set FISH_API_KEY`.

Preferred fix: chown the run directory to ubuntu and launch without sudo:
```
sudo chown -R ubuntu:ubuntu pilot/artifacts/runs/<run-id>/
.venv/bin/python scripts/render_commentary_audio.py ...
```
Less robust alternative: pass env vars explicitly on the sudo command line.

### Evidence directory is root-owned

`/var/lib/sandboxer/evidence/` is owned by root (mode 0700). After a match, ubuntu
cannot write new evidence there directly. Options:
- read with `sudo cat /var/lib/sandboxer/evidence/<match>.result.json`, or
- extract the result JSON from the job log (`pilot/logs/jobs/<match-id>.log`), which
  contains the result.json payload as its final content.

### cmd bridge smoke test is slow under ox-alpha

`cmd -p "CMD_OK" --model stealth/ox-alpha --output-format json ...` took ~112s on this
host. Treat as warm-up; allow 120s+ before concluding the bridge is unreachable.

### Match runner log may be empty during the match

The job log (`pilot/logs/jobs/<match-id>.log`) often stays empty while the match runs;
evidence lands in `/var/lib/sandboxer/evidence/`. An empty log alone is not evidence of
a stuck match — check the runner directory and QEMU process count instead.

## Delegating fixes to OpenCode

See the `opencode` skill for CLI mechanics. Project specifics:

- Work in `/home/ubuntu/projects/SandBoxer`; run tests via
  `cd pilot && .venv/bin/python -m pytest <file> -q`.
- One opencode task per concern, prompt includes: context of the original
  incident, exact deliverables, test file name, "Run pytest …, Do NOT commit".
  The orchestrator (Hermes) verifies tests independently, then commits itself.
- Long tasks take 5–25 min: launch with `terminal(background=true,
  notify_on_complete=true)`, poll progress by grepping the log for plan
  markers and `passed|failed`, and give the user periodic Telegram updates
  (Andrea explicitly asked for frequent progress updates).
