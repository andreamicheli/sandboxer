---
name: sandboxer-pipeline-ops
description: "Run SandBoxer pipeline fixes and releases via opencode."
version: 1.0.0
author: Andrea Micheli (andreamicheli), Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [SandBoxer, OpenCode, Delegation, Publishing, Pipeline]
    related_skills: [opencode, hermes-agent]
---

# SandBoxer Pipeline Ops Skill

Workflow consolidated 2026-08-22 after the P0-P2 hardening series (commits
31ce64c..6874db6): delegating code fixes to OpenCode CLI, commit/test
discipline, and the unlisted-first publication policy. Repo lives at
`~/projects/SandBoxer`; Python package under `pilot/sandboxer_v0/`, tests
under `pilot/tests/`.

## When to Use

- Implementing fixes or features in the SandBoxer pilot pipeline.
- Releasing a run: report first, then unlisted YouTube upload, then gated public flip.
- Don't use for: match execution itself (`pilot/README.md`) or X/social publishing.

## Prerequisites

- OpenCode binary v1.18.x at `/home/ubuntu/.opencode/bin/opencode`. NOT on PATH.
  The other install (`~/.local/bin/opencode`, v0.0.55 Go build) HANGS on `run`.
  Always call the 1.18.x binary by absolute path.
- Config `~/.config/opencode/opencode.jsonc`: model `opencode/x-preview-f-free`
  (this is ox-alpha), small_model `opencode/hy3-free`. No command-code bridge
  in this config (bridge is Hermes-only).
- Test runner: `cd pilot && .venv/bin/python -m pytest <tests> -q`.

## Quick Reference

```bash
# Delegate one bounded task (background, log to file):
~/.opencode/bin/opencode run --title "<task-name>" '<prompt>' > /tmp/oc_<task>.log 2>&1
# Verify before committing:
cd ~/projects/SandBoxer/pilot && .venv/bin/python -m pytest tests/<test>.py -q
# Multiline commit message (from execute_code):
import shlex, subprocess
subprocess.run(["bash","-c","git add <files> && git commit -q -m " + shlex.quote(msg)], cwd="~/projects/SandBoxer")
```

## Procedure

1. Check repo state: `git status --short` + `git log --oneline -3`. Unrelated
   dirty changes get committed separately first (ask owner if unclear).
   Criterion: working tree contains only what the current task owns.
2. Delegate ONE fix per `opencode run` invocation. Prompt must state: files to
   read first, exact deliverable, test file path, the exact pytest command,
   and end with "Do NOT commit". Launch background, redirect to `/tmp/oc_<n>.log`.
   Criterion: process exits 0.
3. Poll progress every few minutes: tail the log (look for its todo list) and
   `git status --short` to see files appearing. Criterion: tests written and run.
4. Verify independently: rerun the pytest command yourself; do not trust the
   agent's reported pass counts alone. Criterion: your own run passes.
5. Commit granularly: stage only the files belonging to that fix, message =
   `feat(scope): summary` + body explaining why. One fix = one commit.
   Criterion: `git log --oneline -1` shows the new commit.
6. Repeat steps 2-5 per fix, in dependency order. After the last one run the
   FULL suite: `.venv/bin/python -m pytest -q`. Criterion: only known
   environmental failures remain.

## Publication Policy (post-release)

Order is enforced by code now; do not bypass manually:

1. Freeze evidence bundle -> report built -> site staged -> Vercel deploy ->
   HTTP 200 verified on `/reports/<slug>`. `publish_gate.py` blocks anything else.
2. Upload YouTube **unlisted automatically** (`privacy_status=public` at
   upload time raises). Owner reviews video + report together.
3. Public flip requires an approval token: `publish_public(video_id, token)`
   with `SANDBOXER_PUBLISH_APPROVAL` env or CLI flag; without it ->
   `ApprovalRequiredError`.

## Engine Defaults

- Editorial phases (commentary, arena, intro, report_narrative): opencode /
  ox-alpha (`x-preview-f-free`).
- Review stages: five DIFFERENT opencode models (nemotron-3-ultra,
  muse-spark-1.2-contributor, hy3-free, big-pickle, nemotron-3.5-lightning);
  ox-alpha excluded so the author never reviews itself.
- Codex/agy/cmd/hermes adapters remain selectable per phase/stage via
  `SANDBOXER_*_AGENT` env overrides when access returns.
- TTS default: Fish Audio `s2.1-pro-free`, voices Kore (play-by-play) /
  Charon (analyst); Gemini only via explicit override.
- Render preflight derives Remotion concurrency from RAM (<6GiB:1, <12:2,
  else 4) and moves tmpfs TMPDIR to persistent disk.

## Pitfalls

- Wrong binary: v0.0.55 silently hangs after "llm runtime selected"; kill and
  switch to the 1.18.x path. Smoke test any binary change with a trivial
  `run 'Respond with exactly: OK'`.
- `terminal()` chokes on multiline quoted git messages; from `execute_code`
  use `subprocess.run(["bash","-c", ...])` with `shlex.quote(msg)` instead.
- `opencode run` takes 5-30 min per fix; use background + notify, peek at the
  log rather than waiting blind.
- Agent may leave pre-existing unrelated diffs; diff each file before staging.
- Full-suite baseline: 595 passed / 0 failed as of 6874db6. Any new failure is
  yours until proven otherwise.

## Verification

- Per-fix: targeted pytest file green, granular commit present.
- Release-ready: full pilot suite green; `python -c` smoke of
  `OpencodeAdapter` against the real binary returns its marker string;
  dry-run upload path still refuses public-at-upload.
