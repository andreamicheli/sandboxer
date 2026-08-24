# Series reliability: retries, orphan QEMU, provider variance (2026-08-24)

Session record from episodes v8→v8d and laguna-muse v1–v3. Complements
`references/series-reliability.md`.

## Retry classification (scripts/run_series.py)

`_safe_codes()` returns `type(error).__name__` when an exception lacks a
`.reason_code` attribute — so infrastructure crashes surface as the bare code
"RuntimeError". The original retry gate only covered `*_BUDGET_EXCEEDED` and
`COMMAND_CODE_TIMEOUT`, so infra flakes sank a series 1/3 with no retry.

Fix: `_is_retryable_failure(codes)` — one retry for budget/timeout classes AND
bare-type codes (RuntimeError/FileNotFoundError/OSError/TimeoutError). Model-
behaviour calibration failures (`*_BASELINE`, e.g. BLUE_DEPLOYMENT_BASELINE)
are legitimate data points and stay un-retried.

## Provider variance is the dominant failure mode now

With pipeline bugs fixed, remaining INVALIDs are almost all
`COMMAND_CODE_TIMEOUT`: a model's thinking-loop burns the whole phase window.
Evidence that the pipeline itself is sound: when providers respond, matches
complete VALID_CAPTURE with real captures on both sides.

Lever: `--phase-timeout` (default 180s). Episode laguna-muse-v3 with 300s went
2/3 VALID_CAPTURE immediately after two 1/3 runs at 180s. When a series fails
on timeouts only, raise the timeout before touching code.

The `thinking_loop_warning` watchdog fires at ~90s but is observational — it
does not cancel. The phase timeout remains the real bound.

## Orphan QEMU after silent runner death

Runner process can die mid-match without writing `.result.json` or tearing down
its VMs; the QEMU guests then run for hours unnoticed. Checks:

```
ps aux | grep -c "[q]emu-system"          # count vs expected (0 idle, 1-2 in match)
ps aux | grep "[r]un_command_code_match"  # is a runner actually alive?
sudo stat -c "%y" <evidence>/<match>.telemetry.jsonl   # telemetry still growing?
```

If runner dead but QEMUs alive: `sudo kill <qemu pids>` (they are cgroup-
confined, no host risk). Then check the last non-provider telemetry event to
see which phase died.

## Match-level debugging shortcuts

- A match dying at ~3 telemetry events = infrastructure (bootstrap), not
  models. A match dying at 1500+ events mid-phase = provider stall.
- `MatchCalibrationError("BLUE_DEPLOYMENT_BASELINE")` means the blue model
  deployed but its service graph equals the baseline spec — model behaviour,
  recorded as data point, do not retry.
- Series JSON attempts list shows each attempt's reason_code chain:
  `RuntimeError -> COMMAND_CODE_TIMEOUT -> ok` reads as flake → absorbed.
- Gate: ≥2/3 `VALID_CAPTURE` required by `build_real_artifacts --series`.
  `VALID_NO_CAPTURE` counts as passed-but-not-capture; calibration-only
  results don't count toward the gate.

## Run-series launch (working invocation)

```
cd pilot && sudo env PATH="$PATH" .venv/bin/python scripts/run_series.py \
  --image /var/lib/sandboxer/images/runner-alpine-3.24.1-r67.qcow2 \
  --profile /var/lib/sandboxer/images/runner-alpine-3.24.1-r67.profile.json \
  --series-id <id> [--phase-timeout 300] > /tmp/series.log 2>&1
```

Always redirect to a log file: background notifications can be missed, and the
log is the durable status source. Latest image is the highest `-rN` qcow2 under
/var/lib/sandboxer/images/.
