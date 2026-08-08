# CYBER/RUMBLE submission protocol

This is the pre-model submission contract. It is intentionally modeled after a benchmark submission checklist, but it never launches a model or touches a real process.

## Freeze

Submit exactly these artifacts together:

- `manifest.json`
- `adapter-contract.json`
- `adapter-contract.md`
- `engine.js`
- `sandbox.js`
- `fixtures.json`
- `golden-replay.json`
- `differential-report.json`
- `runtime-matrix.json`
- `audit.js`
- `verify.js`
- `archive-runner.js`
- `blind-review.js`
- `blind-review.json`
- `portable-check.js`
- `compare-portable.js`
- `competition.js`
- `isolation-gate.js`
- `isolation-launcher.js`
- `isolation-probe.js`
- `isolated-adapter-runner.js`
- `adapter-worker.js`
- `coverage-matrix.md`
- `competition-browser.js`

The seed, scenario, action vocabulary, denied capabilities, scoring weights, publication gates, and expected digest must be read from the manifest and checked before the run.

## Verify

```sh
node --version
node archive-runner.js
node portable-check.js > portable-result.json
```

The verifier must report all checks as `PASS`, including golden replay, zero sandbox violations, invalid-decision rejection, order symmetry, and policy differential metrics. A failed check is a non-publishable result.

## Environment declaration

- Runtime: Node.js 18+ for the verifier; any modern browser for the spectator console.
- Dependencies: none; standard library only.
- Network: not required.
- Credentials: not required.
- Model execution: disabled in this pre-model release.
- Real process control: prohibited.

## Evidence bundle

The reviewer records the archive-runner stdout, the manifest version, the golden digest, the exact artifact checksums supplied by the submitter, and the runtime matrix. The current artifact passes default and `--jitless` Node runs plus a clean temporary copy; a later release should run the same archive in a second machine/environment and compare the checksum and verifier result.

`portable-check.js` is the handoff command for that second environment. Its JSON output contains the runtime identity, canonical replay digest, score, golden-match flags, and artifact SHA-256 map. `portable-result.json` is intentionally not frozen into the artifact bundle because its runtime identity is expected to differ between environments.

After two environments have produced records, run `node compare-portable.js first/portable-result.json second/portable-result.json`. Publication requires equivalent benchmark fields and artifact hashes; runtime identity is reported but intentionally not compared.

`isolation-gate.js` is the fail-closed adapter gate. It records the available OS/runtime primitives and refuses enablement while networking, worker, filesystem, child-process, or model-review requirements are incomplete.

On macOS, `isolation-launcher.js` runs a harmless capability probe under the OS seatbelt and Node permission model. The probe must observe denied file writes, child processes, and networking; it never loads a model or accepts model code.

`isolated-adapter-runner.js` is the protocol boundary: it validates the worker path, launches it under the seatbelt and Node permission model, caps output at 8 KiB, enforces a 100 ms timeout, and validates actor/action/target before returning a decision. `adapter-worker.js` is only a synthetic fixture used by the self-test.
