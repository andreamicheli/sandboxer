# SandBoxer pipeline fixes — incident catalog (2026-08-22 session)

Source: `~/projects/SandBoxer/docs/pipeline-failure-analysis.md` (Laguna vs Muse
run, YouTube `VVuiitjVQVU`). All structural items were fixed in one session via
OpenCode delegation; commit list below.

## Structural fixes (provider-independent) → commits

| Problem | Fix | Commit |
|---|---|---|
| Video uploaded with no report, placeholder URLs | `publish_gate.py` hard gate (bundle + report HTTP 200 + indexed slug); rehearsal/dry-run paths ungated; `allow_ungated` bypass | `31ce64c` |
| Match orchestrator killed with session → orphan QEMU, dup Match ID on retry, unobservable TTS job | `job_runner.py`: setsid detach, PID files under `pilot/logs/jobs/`, status/stop CLI, killpg SIGTERM→SIGKILL, `JobAlreadyRunningError` + `new_job_id()` | `7488c0c` |
| artifacts/ mixed files across runs | `run_layout.py`: `artifacts/runs/<run_id>/`, run-manifest.json sha256 inputs, `validate_run_dir()`; `build_real_artifacts.py --run-id/--artifacts-root` backward compatible | `494b7bd` |
| Telemetry/replay schema mismatch; ad-hoc script had a bug | `artifact_converter.py` canonical schemas + hash roundtrip validation; script delegates to it | `3ce98ae`, `3a8ef47` |
| Commentary overflow, intro offsets in match, hardcoded recap | `schedule.py validate_and_pack(draft, budget)` — clamp/sort/repack/drop, provenance `deterministic_fallback`; recap moved behind manifest fields in index.tsx | `d80ec09` |
| Manifest said Gemini, broadcast record said Fish | TTS requested vs observed provenance + `tts_provider_drift` flag, `validate_provenance()` before upload | `3f064a2` |
| Fish should be default TTS (user rule) | fish/s2.1-pro-free default, Kore/Charon voices; Gemini only explicit override | `af331fe` |
| Remotion OOM (3.7 GB RAM), tmpfs-full /tmp | `render_preflight.py`: concurrency tiers (<6GiB→1,<12→2,else 4; unknown RAM fails closed to 1), tmpfs/<5GiB switch to pilot/logs/render-tmp; auto-injected into remotion command | `54b8bb7` |
| Unlisted-by-default policy + approval for public | uploads default unlisted, public-at-upload refused, `publish_public(video_id, approval_token)` needs `SANDBOXER_PUBLISH_APPROVAL`; publish_broadcast indexes BEFORE upload so gate sees entry | `ad7b398` |

Also committed pre-existing WIP as `74a46b2` before starting.

## Environment notes

- Tests: `cd pilot && .venv/bin/python -m pytest <file> -q`. Full suite ~590 tests.
- Pre-existing failure: `tests/test_setup_credentials.py::test_orca_rejects_missing_key`
  fails on clean HEAD (real orca key in local env). Confirm with stash before blaming changes.
- OpenCode binary lives at `~/.opencode/bin/opencode` (not the tarball at ~/.local/bin).
  Its config is `~/.config/opencode/opencode.jsonc` — model `opencode/x-preview-f-free`
  works; a command-code bridge provider was removed from opencode config per user
  request (bridge only for Hermes). Old bridge config backed up as opencode.jsonc.bridge.bak.
