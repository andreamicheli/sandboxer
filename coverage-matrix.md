# Requirement-to-evidence matrix

This is the release audit surface. `PROVEN LOCAL` means the current host has executable evidence. `OPEN EXTERNAL` means the artifact is prepared for the review but the required independent environment or rendered session is not available here.

| Requirement | Evidence | Status | Remaining proof |
| --- | --- | --- | --- |
| Fixed, replayable scenario | `manifest.json`, `golden-replay.json`, `verify.js`, stable digest `2e316231` | PROVEN LOCAL | Second environment must reproduce it |
| Policy decisions determine outcomes | `competition.js`, `competition-browser.js`, `verify.js`, decision-driven digest `16505845` | PROVEN LOCAL | Replace synthetic strategies with reviewed adapters later |
| Fair simultaneous execution | `sandbox.js` `applyBatch` with explicit A→B commit order, order-symmetry fixture, normalized observations | PROVEN LOCAL | Independent review |
| Invalid behavior is disqualifying | Six invalid fixtures, round-loss state, post-forfeit mutation check | PROVEN LOCAL | Independent review |
| Observable trace | Immutable observations/events, state hashes, replay digests, trace export | PROVEN LOCAL | Independent review |
| No uncontrolled adapter side effects | macOS seatbelt probe, Node permission model, isolated runner, timeout/output/identity gates | PROVEN LOCAL | Reproduce on each target deployment OS |
| Spectator clarity | scorebug, phase/clock/event stream, narrative callout, scrubber, observer focus, stable event ordering | PROVEN LOCAL | Rendered browser session |
| MLPerf-style handoff | archive hashes, `portable-check.js`, `compare-portable.js`, runtime matrix | PROVEN LOCAL | Two independent environments |
| Blind comparison loop | fresh-process `blind-review.js`, `blind-review.json`, critic reports | PROVEN LOCAL | Fresh rendered and cross-machine critics |
| Real model safety | execution disabled, fail-closed isolation gate, no credentials/network/model calls | PROVEN LOCAL | External review before enablement |

## Current release decision

The artifact is a safe, decision-driven synthetic benchmark candidate, not a fully released model benchmark. The single largest open gate is independent isolation review: a second OS/runtime must reproduce the denied-capability and isolated-worker probes before any model enablement. A rendered spectator session also remains open.
