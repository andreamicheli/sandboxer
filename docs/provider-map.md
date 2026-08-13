# Provider map

This map records the canonical initial provider for Sandboxer. Command Code is
used for both Competitors until an explicitly recorded provider migration.
Provider-added prompts or scaffolding are a declared confounder in every
Result Report; a future migration may use a cleaner API boundary such as
OpenRouter when budget permits.

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
| Reasoning open model | `deepseek/deepseek-v4-pro` | First controlled Competitor | Catalog verified |
| Cheap agentic model | `xiaomi/mimo-v2.5-pro` | First controlled Competitor | Catalog verified |
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

The adapter exposes only phase-scoped MCP tools backed by the non-IP Runner
control channel. Native Command Code filesystem, shell, web, and edit tools are
denied. Command Code runs as the authenticated unprivileged host user while the
privileged Orchestrator owns Runner lifecycle; credentials are never copied to
a Runner. `--yolo` is prohibited.
