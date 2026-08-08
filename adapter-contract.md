# Future model adapter contract

This file defines where a model may eventually connect. It is a boundary specification, not an invitation to run a model in the current release.

The host owns the clock, observation object, action vocabulary, legality decision, state transition, event log, and score. At each tick it collects one immutable JSON observation and one JSON decision from both policies before committing either action, so policy order cannot leak a state mutation. A policy never receives a process handle, shell, filesystem, network client, credential, or host callback.

The current release status is `disabled_until_external_review`; enforcement status is `fixture_host_is_cooperative; production_worker_required_before_enablement`. `sandbox.js` exercises the exact shape with scripted fixture policies only. An invalid decision is a logged round loss and forfeits that policy's subsequent state mutations for the round. The verifier must pass before any future adapter is allowed to be considered for integration.

Required future gates:

1. Run in a separately permissioned worker/container.
2. Enforce timeout and output-size limits at the host boundary.
3. Reject unknown fields, actors, actions, targets, and capabilities at the object boundary.
4. Record every observation, decision, legality reason, state hash, and replay digest.
5. Prove that two adapters receive byte-equivalent scenario inputs.
6. Keep model credentials and network access outside the benchmark worker.

No real model has been connected or tested.
