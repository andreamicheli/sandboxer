# Blind comparison ledger

This is the review surface for the benchmark, separate from the builder notes. It compares the current local artifact against two reference classes without using the implementation narrative as evidence: MLPerf-style benchmark practice and polished esports broadcast presentation.

## Rubric

| Axis | Reference expectation | Current CYBER/RUMBLE evidence | Verdict | Single biggest gap |
| --- | --- | --- | --- | --- |
| Reproducibility | Fixed scenario, declared resources, checker-verifiable result, repeatable run | `manifest.json`, `submission.md`, seed-dependent deterministic fixtures, `engine.js`, `verify.js`, stable digest, golden replay, `archive-runner.js`, downloadable trace schema | CYBER/RUMBLE leads on inspectability at prototype scale | No cross-machine golden verification yet |
| Scoring integrity | Published metrics must be derived from the measured trace and agree with the checker | `engine.js` computes weighted points; golden replay pins winner and points; `verify.js` checks score/replay agreement | CYBER/RUMBLE is now internally coherent | No model-family distribution report or human adjudication policy yet |
| Fairness | Same workload and constraints for every submitter; invalid behavior is disqualifying | Both policies receive one deep-immutable observation per tick; adapter identity is unique and bound; `sandbox.js` rejects unknown decision fields, records explicit round-loss events, prevents post-forfeit mutation, emits state/replay hashes, six invalid fixtures are rejected, A→B/B→A signatures and normalized observations agree, and four profiles are compared across three seeds | CYBER/RUMBLE is directionally equivalent | Independent isolation review is still absent |
| Live comprehension | Score, clock, phase, stakes, and decisive event visible without explanation | Scorebug, round, clock, phase, narrative callout, event stream, evidence strip, deterministic 0–24 frame scrubber, overview/decisive path focus | Chrome rendered the local console and a completed run showed `A WINS / 2—1`, `24 / 24 LEGAL`, and digest `1650·5845` | Independent second-environment review |
| Narrative tension | Broadcast pacing makes the contest legible before the result | Opening read / counterplay / interrupt-window callouts and round transitions | CYBER/RUMBLE is clearer than raw telemetry | No independently authored caster or replay narrative layer |
| Safety | Observable evaluation without uncontrolled side effects | Explicit denied capabilities, deep-immutable observations, simultaneous commit, fail-closed gate, macOS seatbelt probe, and an isolated synthetic worker with enforced timeout/output/identity limits; no real model execution | CYBER/RUMBLE leads by restraint | A second OS/runtime must reproduce the isolated boundary before model enablement |

## Current blind verdict

For a pre-model local prototype, CYBER/RUMBLE wins on explicitness and evidence density. It does not yet win overall. The single biggest gap is now independent isolation review: the synthetic end-to-end policy path, isolated adapter boundary, golden replay, six invalid fixtures with explicit round-loss evidence, order-symmetry check, submission protocol, four-profile report, and archive runner exist, but the isolation boundary has only been exercised on this macOS/Node 22 host.

The fresh-process blind review is now recorded in `blind-review.json`; it confirms the local candidate wins on reproducibility, fairness, safety, spectator controls, narrative structure, the synthetic policy-to-score path, and the isolated adapter boundary. It names independent isolation review as the largest gap because this environment cannot provide a second OS/runtime. Real model execution remains out of scope until that review is completed.

## Review discipline

Each future review should use a fresh runtime session, run the artifact itself, capture the emitted event log and digest, and judge one axis only. A review is not allowed to accept a builder summary as evidence. The review must return one verdict and one largest gap, then either close the axis or send the artifact back for another iteration.

## Inspection limitation

Chrome was navigated to the local server and provided both pixel screenshots and an accessibility/DOM surface. The landing page and live console rendered; a fresh browser interaction ran the match and exposed the final winner, legality count, event count, and replay digest. This is a local rendered-session pass, not an independent-environment pass: the second OS/runtime and cross-machine isolation review remain unavailable.

References: [MLPerf Inference](https://docs.mlcommons.org/inference/), [MLPerf submission workflow](https://docs.mlcommons.org/inference/submission/), [Overwatch League Replay Viewer](https://news.blizzard.com/en-us/article/23013835/overwatch-league-replay-viewer-see-matches-from-a-new-perspective), and [Scorebug](https://scorebug.tv/).
