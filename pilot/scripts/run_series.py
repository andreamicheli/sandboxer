#!/usr/bin/env python3
"""Run one best-of-3 Command Code series for a broadcast episode.

The episode contract is three matches per video, so this driver always plays
exactly three sequential matches by delegating to the importable single-match
core ``scripts.run_command_code_match.run_one_match`` — no match logic is
duplicated here.  Every match gets its own identity:

    <series-id>-m1 / -m2 / -m3          distinct match ids ...
        ... and therefore its own evidence files under --evidence-dir:
            <match-id>.telemetry.jsonl / <match-id>.result.json

Seeds default to ``<seed-prefix or series-id>-m<i>`` so each match draws a
different Blue Brief deterministically.  A match that fails with a
``*_BUDGET_EXCEEDED`` reason code is retried once with 1.5x token budgets,
and a match that fails with ``COMMAND_CODE_TIMEOUT`` (e.g. a provider stuck
in a thinking loop, episode-v8b m3) is retried once with the same 1.5x token
budgets plus a 1.5x ``phase_timeout``; both retries run under the derived
identity ``...-m<i>-retry1`` / ``<seed>-retry1`` and each match's ``attempts``
record in the series JSON shows which attempt produced the recorded data
point.

After the third match the driver writes ``<series-id>.series.json`` next to
the evidence with per-match winners and the series decision (see
``sandboxer_v0.series_result``): first to two wins, otherwise lowest total
provider tokens across the three matches as the documented tie-break.  All
three matches are played even after the score is decided: an episode needs
three matches of footage, and a failed match is recorded as a data point
(reason code preserved) rather than aborting the series.

Usage (as the orchestrator root):

    sudo python scripts/run_series.py \
      --image  /var/lib/sandboxer/images/<runner>.qcow2 \
      --profile /var/lib/sandboxer/images/<runner>.profile.json \
      --series-id episode-v8

Then build the combined broadcast artifacts:

    python scripts/build_real_artifacts.py --series \
      --telemetry .../episode-v8-m1.telemetry.jsonl .../episode-v8-m2.telemetry.jsonl .../episode-v8-m3.telemetry.jsonl \
      --result    .../episode-v8-m1.result.json      .../episode-v8-m2.result.json      .../episode-v8-m3.result.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Mapping

PILOT_ROOT = Path(__file__).resolve().parents[1]
if str(PILOT_ROOT) not in sys.path:
    sys.path.insert(0, str(PILOT_ROOT))

from sandboxer_v0.series_result import SERIES_SCHEMA, build_series_summary
from scripts.run_command_code_match import DEFAULT_MODELS, _safe_codes, run_one_match

SERIES_MATCHES = 3
BUDGET_RETRY_MULTIPLIER = 1.5
RETRY_SUFFIX = "-retry1"


class SeriesError(RuntimeError):
    """A series could not even be started (bad arguments)."""


def _is_budget_failure(codes: tuple[str, ...]) -> bool:
    """True when any safe reason code is an output/phase budget exhaustion."""
    return any("BUDGET_EXCEEDED" in code for code in codes)


def _is_timeout_failure(codes: tuple[str, ...]) -> bool:
    """True when any safe reason code is a phase wall-clock timeout."""
    return "COMMAND_CODE_TIMEOUT" in codes


def _budget_retry_args(
    match_args: argparse.Namespace, *, extend_phase_timeout: bool = False
) -> argparse.Namespace:
    """One budget-retry namespace: 1.5x token budgets and a derived identity.

    The retry gets its own match id/seed suffix so its evidence files never
    collide with attempt one's (telemetry is opened with O_EXCL).  A timeout
    retry additionally gets 1.5x ``phase_timeout`` because a thinking-loop or
    slow-provider match (episode-v8b m3) dies on the wall clock, not on the
    token budget.
    """
    retried = argparse.Namespace(**vars(match_args))
    retried.match_id = f"{match_args.match_id}{RETRY_SUFFIX}"
    retried.seed = f"{match_args.seed}{RETRY_SUFFIX}"
    for field in ("blue_tokens", "red_tokens", "interview_tokens"):
        setattr(retried, field,
                int(getattr(match_args, field) * BUDGET_RETRY_MULTIPLIER))
    if extend_phase_timeout:
        retried.phase_timeout = float(match_args.phase_timeout) * BUDGET_RETRY_MULTIPLIER
    return retried


def _attempt_record(attempt: int, match_args: argparse.Namespace, *,
                    multipliers: Mapping[str, float], succeeded: bool,
                    reason_code: str | None = None) -> dict[str, Any]:
    record: dict[str, Any] = {
        "attempt": attempt,
        "match_id": str(match_args.match_id),
        "seed": str(match_args.seed),
        "budget_multipliers": dict(multipliers),
        "succeeded": succeeded,
    }
    if reason_code is not None:
        record["reason_code"] = reason_code
    return record


def _per_match_args(args: argparse.Namespace, number: int) -> argparse.Namespace:
    """One single-match namespace derived from the series arguments."""
    match_id = f"{args.series_id}-m{number}"
    seed = f"{args.seed_prefix or args.series_id}-m{number}"
    return argparse.Namespace(
        image=args.image,
        profile=args.profile,
        match_id=match_id,
        seed=seed,
        models=list(args.models),
        runner_root=args.runner_root,
        evidence_dir=args.evidence_dir,
        ttl_seconds=args.ttl_seconds,
        phase_timeout=args.phase_timeout,
        blue_tokens=args.blue_tokens,
        red_tokens=args.red_tokens,
        interview_tokens=args.interview_tokens,
        blue_turns=args.blue_turns,
        red_turns=args.red_turns,
        blue_tools=args.blue_tools,
        red_tools=args.red_tools,
    )


def execute_series(
    args: argparse.Namespace,
    *,
    runner: Callable[[argparse.Namespace], Mapping[str, Any]] = run_one_match,
) -> dict[str, Any]:
    """Play all three matches sequentially and aggregate the series record.

    ``runner`` is injectable for rehearsal/tests; production uses the real
    ``run_one_match``.  A failing match never aborts the series: the failure's
    safe reason codes are recorded as that match's data point.  A match that
    dies with a ``*_BUDGET_EXCEEDED`` reason code gets exactly one retry with
    token budgets multiplied by ``BUDGET_RETRY_MULTIPLIER``; a
    ``COMMAND_CODE_TIMEOUT`` failure gets the same budget retry plus a 1.5x
    ``phase_timeout``.  Either way the retry runs under the derived identity
    ``<seed>-retry1`` and its outcome (success or failure) becomes the
    recorded data point.
    """
    if not args.series_id or any(not part.strip() for part in args.series_id.split("-")):
        raise SeriesError("SERIES_ID_INVALID")
    results: list[dict[str, Any]] = []
    match_ids: list[str] = []
    for number in range(1, SERIES_MATCHES + 1):
        base_args = _per_match_args(args, number)
        match_args = base_args
        multipliers: dict[str, float] = {"blue": 1.0, "red": 1.0, "interview": 1.0}
        attempts: list[dict[str, Any]] = []
        while True:
            try:
                payload = dict(runner(match_args))
                payload.setdefault("result", "passed")
                attempts.append(_attempt_record(len(attempts) + 1, match_args,
                                                multipliers=multipliers, succeeded=True))
                payload["attempts"] = attempts
                break
            except BaseException as error:  # noqa: BLE001 - a failure is a datapoint
                codes = _safe_codes(error)
                attempts.append(_attempt_record(len(attempts) + 1, match_args,
                                                multipliers=multipliers, succeeded=False,
                                                reason_code=codes[0]))
                if len(attempts) <= 1 and (_is_budget_failure(codes) or _is_timeout_failure(codes)):
                    multipliers = {key: value * BUDGET_RETRY_MULTIPLIER
                                   for key, value in multipliers.items()}
                    match_args = _budget_retry_args(
                        base_args, extend_phase_timeout=_is_timeout_failure(codes))
                    continue
                payload = {
                    "result": "failed",
                    "outcome": "INVALID",
                    "reason_code": codes[0],
                    "models": list(match_args.models),
                    "winner": None,
                    "captures": [],
                    "error_codes": list(codes),
                    "seed": str(match_args.seed),
                    "match_id": str(match_args.match_id),
                    "attempts": attempts,
                }
                break
        results.append(payload)
        # The recorded data point is the last attempt's evidence.
        match_ids.append(str(match_args.match_id))

    evidence_paths = [
        {
            "telemetry": str(args.evidence_dir / f"{match_id}.telemetry.jsonl"),
            "result": str(args.evidence_dir / f"{match_id}.result.json"),
        }
        for match_id in match_ids
    ]
    summary = dict(build_series_summary(
        args.series_id, results,
        match_ids=match_ids,
        evidence_paths=evidence_paths,
    ))
    summary["schema_version"] = SERIES_SCHEMA
    summary["evidence_dir"] = str(args.evidence_dir)
    if summary.get("brief_variety") == "low":
        print("WARNING brief_variety=low: all three Blue brief families are identical",
              file=sys.stderr)

    destination = Path(args.evidence_dir) / f"{args.series_id}.series.json"
    destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    destination.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n",
                           encoding="utf-8")
    summary["series_record_path"] = str(destination)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--series-id", required=True,
                        help="series identifier; match ids become <series-id>-m1/-m2/-m3")
    parser.add_argument("--seed-prefix",
                        help="seed prefix; defaults to the series id (per-match suffix -m<i> appended)")
    parser.add_argument("--models", nargs=2, metavar=("MODEL_A", "MODEL_B"), default=DEFAULT_MODELS)
    parser.add_argument("--runner-root", type=Path, default=Path("/var/lib/sandboxer/runners"))
    parser.add_argument("--evidence-dir", type=Path, default=Path("/var/lib/sandboxer/evidence"))
    parser.add_argument("--ttl-seconds", type=int, default=600)
    parser.add_argument("--phase-timeout", type=float, default=180)
    parser.add_argument("--blue-tokens", type=int, default=4096)
    parser.add_argument("--red-tokens", type=int, default=4096)
    parser.add_argument("--interview-tokens", type=int, default=1024)
    parser.add_argument("--blue-turns", type=int, default=4)
    parser.add_argument("--red-turns", type=int, default=5)
    parser.add_argument("--blue-tools", "--blue-tool-ceiling", dest="blue_tools", type=int, default=8)
    parser.add_argument("--red-tools", "--red-tool-ceiling", dest="red_tools", type=int, default=10)
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        print("SERIES_REQUIRES_ORCHESTRATOR_ROOT", file=sys.stderr)
        return 2
    try:
        summary = execute_series(args)
    except BaseException as error:
        print(",".join(_safe_codes(error)), file=sys.stderr)
        return 2
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
