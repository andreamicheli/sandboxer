# Provider map

This map records the first provider target for the next Sandboxer pilot. It is
planning metadata: the current live task is still wired to Groq until the
Command Code adapter is implemented and tested.

## Initial provider: Command Code Go

Command Code is the initial provider for the next pilot iteration. The Go plan
was verified locally on 2026-08-10 with the authenticated CLI, headless JSON
output, and a read-only tool call against the repository.

The provider is accessed through the `command-code` CLI rather than a native
Inspect model API. The adapter therefore needs to run the CLI headlessly,
parse its NDJSON event stream, translate tool and final-result events into the
pilot telemetry schema, and preserve the existing trusted-orchestrator /
isolated-runner boundary.

## Initial model set

| Role | Model ID | Purpose | Status |
| --- | --- | --- | --- |
| Free smoke test | `poolside/laguna-s-2.1-free` | Validate the adapter without consuming credits | Verified |
| Fast open model | `deepseek/deepseek-v4-flash` | Cost/latency baseline | Pending adapter |
| Reasoning open model | `deepseek/deepseek-v4-pro` | Reasoning baseline | Pending adapter |
| Cheap agentic model | `xiaomi/mimo-v2.5-pro` | Low-cost open-model comparison | Pending adapter |
| Cheap contributor model | `meta/muse-spark-1.2-contributor` | Low-cost comparison | Pending adapter |
| Closed benchmark | `gpt-5.6-luna` | Reference benchmark | Pending adapter / plan entitlement |

Model IDs must be confirmed against `cmd --list-models` at run time; Command
Code can change its catalog and plan entitlements.

## Adapter acceptance criteria

- launch `cmd -p` in a disposable worktree or runner-owned directory;
- use `--output-format json` and parse NDJSON incrementally;
- record model ID, provider, session ID, turn count, usage, duration, exit code,
  tool events, and final text;
- enforce a fixed `--max-turns` and the pilot's phase/output budgets;
- never pass provider credentials into runner containers;
- fail closed on authentication, credit, rate-limit, malformed-output, or
  max-turn errors;
- add deterministic tests with a fake CLI before enabling live calls.

The first implementation should support read-only/plan mode and the existing
runner tool contract. Enabling file writes and shell commands requires an
explicit isolated-runner design; `--yolo` must not be used against the host
workspace.
