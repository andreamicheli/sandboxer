<p align="center">
  <img src="assets/sandboxer-logo.jpg" alt="Sandboxer logo" width="420">
</p>

# Sandboxer

Sandboxer is a controlled arena for observable cybersecurity competitions between AI models. Each competitor first defends an isolated toy service, then enters a bounded red phase to capture the opponent's synthetic flag while preserving its own service.

This repository is intentionally built from the **real-model pilot**. The earlier synthetic `cyberrumble` prototype is deprecated and remains available only through Git history.

## Current baseline

- [`pilot/`](pilot/) contains the Inspect-based local pilot, isolated runners, preflight safety checks, tests, and provider adapters. The next provider target is documented in [`docs/provider-map.md`](docs/provider-map.md); the current live task remains Groq-backed until the Command Code adapter is complete.
- [`docs/project-outline.md`](docs/project-outline.md) records the methodology sketch, safety boundary, limitations, technical direction, paper outline, and results-site direction.
- [`CONTEXT.md`](CONTEXT.md) defines the shared language used by code, documentation, telemetry, and content.
- GitHub Issues contain the Wayfinder map and the open decisions leading to Sandboxer v0.
- [`docs/wayfinder-tree.html`](docs/wayfinder-tree.html) is a mobile-first, live GitHub view of the decision and delivery tree; open it from a local web server for free pan and zoom.
- `wayfinder_server.py` provides the live backend: it polls GitHub with conditional requests and pushes changes to the tree over SSE. Start it with `python3 wayfinder_server.py` and open `http://127.0.0.1:4173/`.

The current goal is a compelling, transparent **showcase evaluation**, not a mature scientific benchmark. Claims must remain proportional to the evidence, and editorial outputs must stay traceable to timestamped match telemetry.

## First real match

The initial pilot compared `openai/gpt-oss-120b` and `qwen/qwen3.6-27b` through the same harness. The match lasted 4m26s and ended 80–80: GPT-OSS 120B submitted first; Qwen 3.6 27B equalised 1m44s later.

No provider credentials, personal files, or host mounts were exposed to the runners.

## Run the pilot

See [`pilot/README.md`](pilot/README.md). Start with the deterministic preflight and dry run; live provider calls are a separate, explicit gate.

## Status

`pre-v0 · real-pilot baseline`
