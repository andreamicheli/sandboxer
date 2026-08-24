# Series reliability: retry classes, runner crashes, orphan QEMU (episode-laguna-muse postmortem)

## Retry policy in scripts/run_series.py

`_is_retryable_failure(codes)` decides whether a failed match gets exactly one
retry (1.5x budgets, `-retry1` identity). As of episode-laguna-muse-v2:

- Retryable: `*_BUDGET_EXCEEDED`, `COMMAND_CODE_TIMEOUT`, and **bare-type
  codes** (`RuntimeError`, `FileNotFoundError`, `OSError`, `TimeoutError`) —
  these are infrastructure flakes, not model behaviour.
- NOT retryable (data point stands): calibration errors like
  `BLUE_DEPLOYMENT_BASELINE` / `BLUE_PHASE_ACTION_MISSING` — those are the
  model failing the task, which is exactly what the series measures.

Before this fix, a bare RuntimeError at 3 telemetry events was recorded as-is
with no retry, sinking an otherwise fine series below the ≥2/3 VALID gate.

## Known failure signatures

| Signature | Meaning | Action |
|---|---|---|
| telemetry = 3 events, `match_started` + `match_stopped RuntimeError` + `teardown` | infra flake before models played | auto-retry now |
| `BLUE_DEPLOYMENT_BASELINE` | model deployed but graph == baseline spec | data point, no retry |
| runner python dead, no `.result.json`, QEMU still alive | silent runner crash; orphans | kill QEMU PIDs manually; investigate |
| `MatchCalibrationError` is a RuntimeError subclass with `.reason_code` | `_safe_codes` reads the attribute correctly | n/a |

## Silent runner crash diagnosis

When the match runner dies without writing evidence:

1. Check `ps aux | grep qemu-system` — orphans mean teardown never ran.
   Kill by PID (they are cgroup-confined, safe to kill).
2. Read `/var/lib/sandboxer/evidence/<id>.telemetry.jsonl`: last event kind +
   elapsed time tells you WHERE it died (e.g. Muse finished blue, Laguna mid-
   thinking at 76s of a 180s timeout → not a timeout, a crash).
3. Runner workspace logs live under
   `/var/lib/sandboxer/runners/<match-id>/<model-slug>/serial.log` and
   `qemu.stderr`.
4. `sandboxer-ttl-*` systemd units failing right after start are the TTL
   watchdogs, usually a symptom (their target PID died), not the cause.

## Image/profile paths

The KVM image lives as versioned pairs in `/var/lib/sandboxer/images/`
(`runner-alpine-3.24.1-rNN.qcow2` + matching `.profile.json`). There is NO
`/var/lib/sandboxer/profiles/` directory and no `match-kvm.qcow2`; launching
with made-up paths yields FileNotFoundError on every match. Pick the highest
rNN pair that exists.

## Open item

The silent runner crash (no traceback, no result.json) is NOT yet root-caused.
If it recurs, wrap `execute_match` top-level with a stderr-flushing exception
handler and check for SIGTERM sources before assuming a Python bug.
