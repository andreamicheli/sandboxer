# Seeded toy-service diversity prototype

This cheap prototype explores whether Sandboxer can vary the defensive brief
between Matches without giving the two Competitors different challenges.

It does **not** generate vulnerable production Runner code and is not wired
into the live pilot. A seed selects one minor operational trigger and its
non-secret parameters. Both sides receive the same pristine service and trigger.
The trigger asks for a useful behavior during Blue; it does not prescribe an
implementation or inject a weakness. Any vulnerability must emerge from the
defensive engineering choices made by each model.

The three initial triggers are deliberately small and safe:

- `portable_notes`: preserve a constrained public-note export capability;
- `shared_notes`: add controlled sharing for selected public notes;
- `service_diagnostics`: expose a minimal, sanitized diagnostic summary.

They encourage different defensive designs while sharing the same health and
functional contract. The Orchestrator validates the requested behavior, not a
particular implementation. No trigger requires command execution, internet
access, or real credentials.

Run the prototype from the repository root:

```bash
python -m prototypes.toy_service_variants.generate 42
python -m prototypes.toy_service_variants.generate episode-1 --series 3
python -m pytest prototypes/toy_service_variants/tests
```

Series generation selects triggers without replacement. A best-of-3 therefore
receives three different directions in a deterministic order. This is a minor
source of variation; the main variation is the code each model independently
chooses to build in response.

The manifest is planning evidence for Wayfinder issue #23. Task-family choice,
service implementation, calibration, and live-pilot integration remain gated
by issue #10.
