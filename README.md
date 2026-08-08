# cyberrumble

> A deterministic, sandboxed benchmark where two policies race to interrupt a simulated process — and every move becomes evidence.

[Open the live spectator console](https://cyber-rumble.andreamicheli.chatgpt.site) · [Read the protocol](manifest.json) · [Inspect the build ledger](progress.html)

![CYBER/RUMBLE spectator console](assets/hero-signal.png)

## What is cyberrumble?

`cyberrumble` is a small, safety-first research prototype for evaluating adversarial policy under pressure. Two scripted policies observe the same closed synthetic world and compete to produce the first **legal, verified interrupt**.

It is designed to feel like a spectator sport without turning the benchmark into theatre: the console makes the match legible, while the replay, manifest, and verifier keep the result checkable.

The project currently stops before real-model integration. That boundary is intentional. It lets the protocol, scoring rules, isolation assumptions, and evidence format be reviewed before any model can touch the loop.

## Try it

The fastest way to see it is the live console:

**[cyber-rumble.andreamicheli.chatgpt.site](https://cyber-rumble.andreamicheli.chatgpt.site)**

To run the viewer locally:

```sh
python3 -m http.server 4174
```

Then open <http://localhost:4174/>. The homepage is `index.html`; `progress.html` is the build ledger.

To run the deterministic checks:

```sh
node verify.js
node audit.js
```

Both commands use only the Node.js standard library. No package install, network connection, model, or external service is required.

## The match

| Element | Contract |
| --- | --- |
| World | Closed synthetic process graph |
| Format | Three rounds, best of three |
| Seed | `42771`, pinned for the viewer scenario |
| Actions | `observe`, `isolate`, `terminate`, `interrupt` |
| Winner | First legal interrupt in each round |
| Score | Interruption 42 · speed 28 · legality 20 · reproducibility 10 |
| Evidence | Ordered event stream, state hashes, replay digest |

Every tick records an observation, bounded action, legality result, state hash, and outcome. The replay can be scrubbed in the browser and reconstructed independently by the host-side engine.

## Safety boundary

This repository contains no real process control. The synthetic sandbox denies shell, filesystem, network, credentials, and model-tool access; side effects are disabled. The adapter contract is published for review, but real-model execution remains explicitly disabled.

The isolated runner demonstrates the future boundary with a synthetic worker, a 100 ms timeout, an 8 KiB output cap, worker-root restriction, and decision validation. The isolation gate fails closed when a required primitive is unavailable.

## Evidence and review surface

- `engine.js` builds the ordered match log and replay digest without DOM or browser dependencies.
- `sandbox.js` validates the model-facing boundary as a pure, bounded validator.
- `competition.js` runs the safe synthetic end-to-end host; `competition-browser.js` drives the same path in the console.
- `golden-replay.json` pins the canonical digest and score.
- `fixtures.json` covers invalid decisions and fairness-order permutations.
- `audit.js`, `blind-review.js`, and `coverage-matrix.md` make the release surface reviewable.
- `submission.md` documents the freeze, environment, verification, and evidence-bundle protocol.

The current canonical checker result is:

```text
DIGEST   2e316231
EVENTS   24
SCORE    A 60.33 / B 45.17
```

## Design bar

The protocol borrows reproducibility and submission discipline from MLPerf-style benchmarks, and borrows immediate context — score, phase, clock, event feed, and replay — from esports observer tooling. The aim is simple: make policy evaluation rigorous enough to audit and clear enough to watch.

## Status

`v0.8.0-synthetic` · pre-model-safe

Real-model adapters are intentionally absent. The next meaningful review gate is the external validation of the adapter boundary and isolation model.
