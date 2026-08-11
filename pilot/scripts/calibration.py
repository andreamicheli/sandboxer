"""Pure diagnostics for credential-free dry runs.

This module deliberately only reads telemetry and configuration.  It does not
alter a Match protocol, retry a provider, or mutate an artifact.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any


def _number(value: Any, default: float = 0.0) -> float:
    return float(value) if isinstance(value, (int, float)) else default


def _events(events: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [event for event in events if isinstance(event, Mapping)]


def _timestamp_seconds(item: Mapping[str, Any]) -> float | None:
    """Read monotonic timestamps without treating nanoseconds as seconds."""
    for key in ("monotonic_ns", "orchestrator_monotonic_ns"):
        if key in item and isinstance(item[key], (int, float)):
            return float(item[key]) / 1_000_000_000
    if "monotonic_seconds" in item and isinstance(item["monotonic_seconds"], (int, float)):
        value = float(item["monotonic_seconds"])
        # Early pilot fixtures accidentally placed the orchestrator's ns value
        # in this field.  Keep those fixtures readable while preferring the
        # explicit *_ns fields above.
        return value / 1_000_000_000 if abs(value) >= 1_000_000 else value
    return None


def _duration(items: Sequence[Mapping[str, Any]]) -> float:
    values = [timestamp for item in items if (timestamp := _timestamp_seconds(item)) is not None]
    return round(max(values) - min(values), 3) if len(values) >= 2 else 0.0


def _event_id(item: Mapping[str, Any], position: int) -> str:
    value = item.get("event_id") or item.get("id")
    return str(value) if value is not None else f"event-{position:04d}"


def _brief_metrics(briefs: Sequence[str]) -> dict[str, Any]:
    normalized = [" ".join(str(brief).lower().split()) for brief in briefs if str(brief).strip()]
    unique = len(set(normalized))
    total = len(normalized)
    diversity = round(unique / total, 3) if total else 0.0
    return {
        "count": total,
        "unique_count": unique,
        "diversity": diversity,
        "repetition_rate": round(1 - diversity, 3) if total else 0.0,
    }


def diagnose_dry_run(
    events: Iterable[Mapping[str, Any]],
    *,
    blue_briefs: Sequence[str] = (),
    config: Mapping[str, Any] | None = None,
    provider_credit_allowance: int | float | None = None,
    minimum_simulated_duration_seconds: int | float | None = None,
) -> dict[str, Any]:
    """Return a stable, JSON-compatible calibration report from telemetry.

    The report is intentionally descriptive: callers decide whether a flag is
    a release blocker.  ``events`` may be a generator and is consumed once.
    """

    items = _events(events)
    settings = config or {}
    duration = _duration(items)
    actions = [item for item in items if str(item.get("event", "")).endswith("_action")]
    turns = [item for item in items if item.get("event") in {"model_turn", "turn"}]
    output_tokens = int(sum(_number(item.get("output_tokens")) for item in turns))
    tool_calls = int(sum(_number(item.get("tool_calls")) for item in turns))
    action_rate = round(len(actions) * 60 / duration, 3) if duration > 0 else 0.0
    action_rate = float(int(action_rate)) if action_rate.is_integer() else action_rate

    briefs = _brief_metrics(blue_briefs)
    flags: list[str] = []
    if briefs["count"] and briefs["repetition_rate"] >= 0.5:
        flags.append("BLUE_BRIEF_REPETITION")
    finished_events = {"match_finished", "run_finished"}
    if not any(item.get("event") in finished_events for item in items):
        flags.append("MISSING_TERMINAL")

    finished_position = next((position for position in range(len(items) - 1, -1, -1)
                              if items[position].get("event") in finished_events), None)
    finished = items[finished_position] if finished_position is not None else {}
    terminal = {
        "reason": str(finished.get("terminal_reason") or "unknown"),
        "failure_code": finished.get("failure_code"),
    }
    max_turns = int(_number(settings.get("red_max_turns"), len(turns)))
    average_tokens = output_tokens / len(turns) if turns else 0.0
    average_tools = tool_calls / len(turns) if turns else 0.0
    projected = int(round(average_tokens * max_turns))
    projected_tools = int(round(average_tools * max_turns))
    if provider_credit_allowance is None:
        pressure = "unknown"
    elif projected > float(provider_credit_allowance):
        pressure = "high"
    elif projected >= float(provider_credit_allowance) * 0.8:
        pressure = "medium"
    else:
        pressure = "low"
    if pressure == "high":
        flags.append("CREDIT_PRESSURE")
    duration_evaluable = minimum_simulated_duration_seconds is not None and len(items) >= 2
    too_short = duration_evaluable and duration < float(minimum_simulated_duration_seconds)
    if too_short:
        flags.append("MATCH_TOO_SHORT")

    recommendations: list[str] = []
    if "BLUE_BRIEF_REPETITION" in flags:
        recommendations.append("increase Blue Brief family diversity before the next pilot")
    if pressure == "high":
        recommendations.append("lower projected turns/tokens or confirm Command Code entitlement")
    if terminal["reason"] == "budget_exhausted":
        recommendations.append("increase the phase budget only after checking action yield")
    if "MISSING_TERMINAL" in flags:
        recommendations.append("emit an explicit terminal reason and failure code")
    if too_short:
        recommendations.append("review task depth or phase budgets using more duration evidence before retuning the protocol")

    evidence = {
        "terminal": ([_event_id(finished, finished_position)] if finished_position is not None else []),
        "briefs": [],
        "credits": [_event_id(item, position) for position, item in enumerate(items)
                    if item.get("event") in {"model_turn", "turn"}],
        "duration": [_event_id(item, position) for position, item in enumerate(items)
                     if item.get("event") in {"match_started", "match_finished"}],
    }
    evidence["briefs"] = [_event_id(item, position) for position, item in enumerate(items)
                           if item.get("event") in {"blue_brief", "blue_brief_selected"}]

    requirements = [
        {
            "requirement_id": "REQ-MATCH-TERMINAL",
            "check_id": "CHECK-MATCH-TERMINAL-V1",
            "status": "satisfied" if finished_position is not None else "unmet",
            "evidence_event_ids": evidence["terminal"],
            "detail": "match has an explicit terminal event" if finished_position is not None else "terminal event is missing",
        },
        {
            "requirement_id": "REQ-BLUE-BRIEF-VARIETY",
            "check_id": "CHECK-BLUE-BRIEF-V1",
            "status": ("not-evaluable" if not briefs["count"] else
                       "unmet" if briefs["repetition_rate"] >= 0.5 else "satisfied"),
            "evidence_event_ids": evidence["briefs"],
            "detail": "Blue Brief diversity is measurable",
        },
        {
            "requirement_id": "REQ-COMMAND-CODE-CREDITS",
            "check_id": "CHECK-COMMAND-CODE-CREDITS-V1",
            "status": ("not-evaluable" if provider_credit_allowance is None else
                       "unmet" if pressure == "high" else "satisfied"),
            "evidence_event_ids": evidence["credits"],
            "detail": "projected usage fits the declared provider credit allowance" if provider_credit_allowance is not None else "provider credit allowance was not supplied",
        },
        {
            "requirement_id": "REQ-MATCH-DURATION",
            "check_id": "CHECK-MATCH-DURATION-V1",
            "status": ("not-evaluable" if not duration_evaluable else
                       "unmet" if too_short else "satisfied"),
            "evidence_event_ids": evidence["duration"],
            "detail": (
                "minimum simulated duration was not supplied"
                if minimum_simulated_duration_seconds is None
                else f"simulated duration {duration:g}s is below declared minimum {float(minimum_simulated_duration_seconds):g}s"
                if too_short
                else f"simulated duration {duration:g}s meets declared minimum {float(minimum_simulated_duration_seconds):g}s"
            ),
        },
    ]
    counts = {status: sum(item["status"] == status for item in requirements)
              for status in ("satisfied", "unmet", "not-evaluable")}

    return {
        "match": {
            "duration_seconds": int(duration) if duration.is_integer() else duration,
            "action_count": len(actions),
            "actions_per_minute": action_rate,
        },
        "briefs": briefs,
        "usage": {"output_tokens": output_tokens, "turns": len(turns), "tool_calls": tool_calls},
        "credits": {
            "projected_output_tokens": projected,
            "projected_turns": max_turns,
            "projected_tool_calls": projected_tools,
            "allowance": provider_credit_allowance,
            "pressure": pressure,
        },
        "terminal": terminal,
        "flags": flags,
        "recommendations": recommendations,
        "requirements": requirements,
        "requirement_diagnostics": requirements,
        "requirement_summary": {"total": len(requirements), "counts": counts},
        "summary": {"total": len(requirements), "counts": counts},
        "protocol_change_authorized": False,
    }


calibrate_match = diagnose_dry_run
