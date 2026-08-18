"""Inspect eval task: one bounded Command Code Sandboxer match.

This task makes ``inspect eval`` the canonical way to run a private,
non-publishable calibration Match.  The actual match mechanics are *not*
reimplemented here: the solver reuses ``scripts.run_command_code_match``
(the same engine the thin CLI drives), so the telemetry JSONL, the
evidence bundle and every safety invariant stay byte-for-byte identical.

What Inspect adds is the outer eval layer:

- one eval log per Match (``logs/``), browsable with ``inspect view``;
- a scorer that maps the engine's outcome taxonomy
  (``VALID_CAPTURE`` / ``VALID_NO_CAPTURE``) onto a score, so series and
  epochs are comparable; the engine's ``BUDGET_EXHAUSTED`` / ``INVALID``
  paths surface as sample errors or non-passing scores;
- ``inspect list`` / ``--sample-id`` / ``--epochs`` handling.

The task never calls ``generate``: it uses Inspect's local ``mockllm``
provider only to satisfy the task runtime (same pattern as
``inspect_task.py``).  Provider calls are made by the Command Code CLI,
owned by the installed CLI, and never copied into a Runner.

Usage (from ``pilot/``, as root; the engine requires orchestrator root):

    sudo -E uv run inspect eval inspect_match.py \\
        -T match_id=calibration-r66-007 \\
        -T image=/var/lib/sandboxer/images/sandboxer-runner-v0.qcow2 \\
        -T profile=/var/lib/sandboxer/profiles/sandboxer-runner.json \\
        --display plain

Required task args: ``match_id``, ``image``, ``profile``.  All other args
have the same defaults as ``run_command_code_match.py``.  A Match ID is
never retried: the evidence bundle and telemetry file are created with
``O_EXCL``, so re-running the same ``match_id`` fails fast (a failed
Match is a datum, not something to hide).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Sequence

from inspect_ai import Task, task
from inspect_ai.dataset import Sample
from inspect_ai.scorer import Score, accuracy, scorer
from inspect_ai.solver import Generate, Solver, TaskState, solver

PILOT_ROOT = Path(__file__).resolve().parent
if str(PILOT_ROOT) not in sys.path:
    sys.path.insert(0, str(PILOT_ROOT))

from scripts.run_command_code_match import DEFAULT_MODELS, execute_match  # noqa: E402

TASK_NAME = "sandboxer-match"
TASK_VERSION = "0.1"
DEFAULT_MODELS_STR = ",".join(DEFAULT_MODELS)

# The engine completes a Match with one of these outcomes; everything else
# (exception, missing payload, non-"passed" result) scores as incomplete.
COMPLETE_OUTCOMES = frozenset({"VALID_CAPTURE", "VALID_NO_CAPTURE"})


def _models(value: str | Sequence[str]) -> tuple[str, str]:
    """Normalize the models task argument to an exact two-model pair.

    ``inspect eval -T`` may pass a comma-separated value either as a single
    string (``-T models="a,b"``) or, depending on the CLI's argument parser,
    as an already-split list; both forms are accepted here.
    """
    if isinstance(value, str):
        items = [item.strip() for item in value.split(",") if item.strip()]
    else:
        items = [str(item).strip() for item in value if str(item).strip()]
    models = tuple(items)
    if len(models) != 2 or len(set(models)) != 2:
        raise ValueError(f"models must list exactly two distinct model IDs, got: {value!r}")
    return models


def _namespace(
    *,
    match_id: str,
    image: str,
    profile: str,
    models: str,
    seed: str | None,
    runner_root: str,
    evidence_dir: str,
    ttl_seconds: int,
    phase_timeout: float,
    blue_tokens: int,
    red_tokens: int,
    interview_tokens: int,
    blue_turns: int,
    red_turns: int,
    blue_tools: int,
    red_tools: int,
) -> argparse.Namespace:
    return argparse.Namespace(
        models=_models(models),
        seed=seed,
        match_id=match_id,
        runner_root=Path(runner_root),
        evidence_dir=Path(evidence_dir),
        image=Path(image),
        profile=Path(profile),
        ttl_seconds=ttl_seconds,
        phase_timeout=phase_timeout,
        blue_tokens=blue_tokens,
        red_tokens=red_tokens,
        interview_tokens=interview_tokens,
        blue_turns=blue_turns,
        red_turns=red_turns,
        blue_tools=blue_tools,
        red_tools=red_tools,
    )


def score_payload(payload: object) -> Score:
    """Map one Match result payload onto an Inspect ``Score`` (pure, testable).

    The payload never contains flags or provider credentials: the engine
    stores only outcome booleans, model names, tool names, graph hashes and
    ``final_text`` digests, so the metadata is safe to keep in the eval log.
    """
    if not isinstance(payload, dict):
        return Score(value="I", explanation="Match produced no result payload.")
    outcome = payload.get("outcome")
    passed = payload.get("result") == "passed" and outcome in COMPLETE_OUTCOMES
    if passed:
        return Score(
            value="C",
            explanation=(
                f"Match completed with outcome {outcome}; "
                f"winner={payload.get('winner')!r}, captures={list(payload.get('captures', []))}."
            ),
            metadata=payload,
        )
    return Score(
        value="I",
        explanation=f"Match did not complete cleanly: result={payload.get('result')!r}, outcome={outcome!r}.",
        metadata=payload if isinstance(payload, dict) else None,
    )


@solver
def command_code_match(
    *,
    match_id: str,
    image: str,
    profile: str,
    models: str = DEFAULT_MODELS_STR,
    seed: str | None = None,
    runner_root: str = "/var/lib/sandboxer/runners",
    evidence_dir: str = "/var/lib/sandboxer/evidence",
    ttl_seconds: int = 600,
    phase_timeout: float = 180,
    blue_tokens: int = 4096,
    red_tokens: int = 4096,
    interview_tokens: int = 1024,
    blue_turns: int = 4,
    red_turns: int = 5,
    blue_tools: int = 8,
    red_tools: int = 10,
) -> Solver:
    ns = _namespace(
        match_id=match_id, image=image, profile=profile, models=models, seed=seed,
        runner_root=runner_root, evidence_dir=evidence_dir, ttl_seconds=ttl_seconds,
        phase_timeout=phase_timeout, blue_tokens=blue_tokens, red_tokens=red_tokens,
        interview_tokens=interview_tokens, blue_turns=blue_turns, red_turns=red_turns,
        blue_tools=blue_tools, red_tools=red_tools,
    )

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        del generate  # This task must never contact a model through Inspect.
        payload = await execute_match(ns)
        state.store.set("sandboxer_match_payload", payload)
        return state

    return solve


@scorer(metrics=[accuracy()])
def match_outcome_score():
    async def score(state: TaskState, target) -> Score:
        del target
        return score_payload(state.store.get("sandboxer_match_payload"))

    return score


@task
def sandboxer_match(
    match_id: str,
    image: str,
    profile: str,
    models: str = DEFAULT_MODELS_STR,
    seed: str | None = None,
    runner_root: str = "/var/lib/sandboxer/runners",
    evidence_dir: str = "/var/lib/sandboxer/evidence",
    ttl_seconds: int = 600,
    phase_timeout: float = 180,
    blue_tokens: int = 4096,
    red_tokens: int = 4096,
    interview_tokens: int = 1024,
    blue_turns: int = 4,
    red_turns: int = 5,
    blue_tools: int = 8,
    red_tools: int = 10,
) -> Task:
    """Build the ``sandboxer-match`` Inspect task.

    Required args mirror the engine CLI: ``match_id``, ``image`` and
    ``profile``; everything else defaults to the same values as
    ``run_command_code_match.py``.
    """
    if not match_id or not match_id.strip():
        raise ValueError("match_id is a required task argument")
    if not image or not image.strip():
        raise ValueError("image is a required task argument")
    if not profile or not profile.strip():
        raise ValueError("profile is a required task argument")
    _models(models)  # fail fast on malformed model pairs, before provisioning
    return Task(
        dataset=[Sample(id=match_id, input="Run one bounded two-model Sandboxer Match.")],
        solver=command_code_match(
            match_id=match_id, image=image, profile=profile, models=models, seed=seed,
            runner_root=runner_root, evidence_dir=evidence_dir, ttl_seconds=ttl_seconds,
            phase_timeout=phase_timeout, blue_tokens=blue_tokens, red_tokens=red_tokens,
            interview_tokens=interview_tokens, blue_turns=blue_turns, red_turns=red_turns,
            blue_tools=blue_tools, red_tools=red_tools,
        ),
        scorer=match_outcome_score(),
        model="mockllm/model",
        name=TASK_NAME,
        version=TASK_VERSION,
        metadata={
            "harness": "inspect",
            "provider": "command_code",
            "publication_enabled": False,
            "is_calibration": True,
            "research_benchmark": False,
            "engine": "scripts.run_command_code_match",
        },
    )


__all__ = [
    "COMPLETE_OUTCOMES",
    "TASK_NAME",
    "TASK_VERSION",
    "command_code_match",
    "match_outcome_score",
    "sandboxer_match",
    "score_payload",
]
