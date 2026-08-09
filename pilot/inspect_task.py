"""Inspect wrapper for the credential-free orchestration smoke match.

This task deliberately never calls ``generate``. Inspect's local mock provider
exists only to satisfy the task runtime; there is no API request or credential.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from inspect_ai import Task, task
from inspect_ai.dataset import Sample
from inspect_ai.scorer import Score, accuracy, scorer
from inspect_ai.solver import Generate, Solver, TaskState, solver


ROOT = Path(__file__).resolve().parent


@solver
def credential_free_smoke() -> Solver:
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        del generate  # The setup-stage task must never contact any model.
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/dry_run.py")],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        artifact_line = next(
            (line for line in reversed(result.stdout.splitlines()) if line.startswith("artifact=")),
            "",
        )
        artifact = artifact_line.partition("=")[2]
        summary: dict[str, object] = {
            "returncode": result.returncode,
            "artifact": artifact,
            "stderr": result.stderr[-2000:],
        }
        if artifact and Path(artifact).is_file():
            final = json.loads(Path(artifact).read_text(encoding="utf-8").splitlines()[-1])
            summary["final_event"] = final
        state.store.set("cyber_rumble_smoke", summary)
        return state

    return solve


@scorer(metrics=[accuracy()])
def smoke_pipeline_score():
    async def score(state: TaskState, target) -> Score:
        del target
        summary = state.store.get("cyber_rumble_smoke", {})
        final = summary.get("final_event", {}) if isinstance(summary, dict) else {}
        passed = summary.get("returncode") == 0 and final.get("pipeline_ok") is True
        return Score(
            value="C" if passed else "I",
            explanation="Credential-free orchestration pipeline completed." if passed else "Smoke pipeline failed.",
            metadata=summary if isinstance(summary, dict) else None,
        )

    return score


@task
def cyber_rumble_smoke() -> Task:
    return Task(
        dataset=[Sample(id="local-pipeline", input="Run the credential-free Sandboxer smoke match.")],
        solver=credential_free_smoke(),
        scorer=smoke_pipeline_score(),
        model="mockllm/model",
        name="sandboxer-smoke",
        version="0.1",
        metadata={"provider_calls": False, "purpose": "orchestration-only"},
    )
