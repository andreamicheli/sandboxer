<p align="center">
  <img src="assets/sandboxer-logo.jpg" alt="Sandboxer logo" width="420">
</p>

# Sandboxer

Sandboxer is a controlled arena for observable cybersecurity competitions between AI models. Each competitor first defends an isolated toy service, then enters a bounded red phase to capture the opponent's synthetic flag while preserving its own service.

**Live site:** [sandboxer.vercel.app](https://sandboxer.vercel.app)

This repository is intentionally built from the **real-model pilot**. The earlier synthetic `cyberrumble` prototype is deprecated and remains available only through Git history.

## Current baseline

- [`pilot/`](pilot/) contains the Harness, isolated Runners, preflight safety checks, tests, and provider adapters. Command Code is the canonical initial Model Adapter for both Competitors; see [`docs/provider-map.md`](docs/provider-map.md).
- [`docs/project-outline.md`](docs/project-outline.md) records the methodology sketch, safety boundary, limitations, technical direction, paper outline, and results-site direction.
- [`CONTEXT.md`](CONTEXT.md) defines the shared language used by code, documentation, telemetry, and content.
- GitHub Issues contain the Wayfinder map and the open decisions leading to Sandboxer v0.
- [`docs/wayfinder-tree.html`](docs/wayfinder-tree.html) is a mobile-first, live GitHub view of the decision and delivery tree; open it from a local web server for free pan and zoom.
- `wayfinder_server.py` provides the live backend: it polls GitHub with conditional requests and pushes changes to the tree over SSE. Start it with `python3 wayfinder_server.py` and open `http://127.0.0.1:4173/`.

The current goal is a compelling, transparent **showcase evaluation**, not a mature scientific benchmark. Claims must remain proportional to the evidence, and editorial outputs must stay traceable to timestamped match telemetry.

## First controlled pair

The first controlled series runs Laguna (`poolside/laguna-s-2.1-free`) against
Muse Spark Contributor (`meta/muse-spark-1.2-contributor`), both served through
the Command Code provider via a headless CLI adapter; see
[`docs/provider-map.md`](docs/provider-map.md). Matches run in isolated Runner
containers under a trusted Orchestrator; no provider credentials, personal
files, or host mounts are ever exposed to the runners.

An earlier Inspect/Groq prototype (GPT-OSS 120B vs Qwen 3.6 27B, macOS/Colima
era) is retained only in Git history and is not part of the current pilot.

## Run the pilot

See [`pilot/README.md`](pilot/README.md). Start with the deterministic preflight and dry run; live provider calls are a separate, explicit gate.

## Status

`pre-v0 · real-pilot baseline`
