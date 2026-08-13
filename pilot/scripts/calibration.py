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


def _diversity_metrics(items: Sequence[str]) -> dict[str, Any]:
    normalized = [str(item).strip() for item in items if str(item).strip()]
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
    defense_policies: Sequence[str] = (),
    deployment_graphs: Sequence[str] = (),
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

    promoted_events = [item for item in items if item.get("event") == "deployment_promoted" or item.get("kind") == "deployment_promoted"]
    observed_policies = (
        list(defense_policies) if defense_policies
        else [str(item["protected_policy"]) for item in promoted_events if "protected_policy" in item]
    )
    observed_graphs = (
        list(deployment_graphs) if deployment_graphs
        else [str(item["graph_hash"]) for item in promoted_events if "graph_hash" in item]
    )
    policies_metric = _diversity_metrics(observed_policies)
    graphs_metric = _diversity_metrics(observed_graphs)

    flags: list[str] = []
    if briefs["count"] and briefs["repetition_rate"] >= 0.5:
        flags.append("BLUE_BRIEF_REPETITION")

    policy_repetition = policies_metric["count"] >= 2 and (
        policies_metric["repetition_rate"] >= 0.5 or policies_metric["unique_count"] < 2
    )
    if policy_repetition:
        flags.append("DEFENSE_POLICY_REPETITION")

    graph_repetition = graphs_metric["count"] >= 2 and (
        graphs_metric["repetition_rate"] >= 0.5 or graphs_metric["unique_count"] < 2
    )
    if graph_repetition:
        flags.append("DEPLOYMENT_GRAPH_REPETITION")

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
    if policy_repetition:
        recommendations.append(
            f"observed repeated defense policy ({policies_metric["unique_count"]}/{policies_metric["count"]} unique) in declared sample; "
            "limited-sample evidence indicates insufficient defense diversity and requires harness/brief retune without forcing model policy choice"
        )
    if graph_repetition:
        recommendations.append(
            f"observed repeated deployment graphs ({graphs_metric["unique_count"]}/{graphs_metric["count"]} unique) in declared sample; "
            "limited-sample evidence indicates insufficient graph diversity and requires harness/brief retune"
        )
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
        "policy": [_event_id(item, position) for position, item in enumerate(items)
                   if (item.get("event") == "deployment_promoted" or item.get("kind") == "deployment_promoted")
                   and "protected_policy" in item],
        "graph": [_event_id(item, position) for position, item in enumerate(items)
                  if (item.get("event") == "deployment_promoted" or item.get("kind") == "deployment_promoted")
                  and "graph_hash" in item],
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

    if policies_metric["count"] > 0:
        requirements.append({
            "requirement_id": "REQ-DEFENSE-POLICY-DIVERSITY",
            "check_id": "CHECK-DEFENSE-POLICY-DIVERSITY-V1",
            "status": "unmet" if policy_repetition else "satisfied",
            "evidence_event_ids": evidence["policy"] or evidence["terminal"] or evidence["duration"],
            "detail": (
                f"observed repeated defense policy in declared sample (count={policies_metric["count"]}, unique={policies_metric["unique_count"]}); limited-sample evidence requires retune"
                if policy_repetition
                else f"defense policy diversity meets sample threshold (count={policies_metric["count"]}, unique={policies_metric["unique_count"]})"
            ),
        })

    if graphs_metric["count"] > 0:
        requirements.append({
            "requirement_id": "REQ-DEPLOYMENT-GRAPH-DIVERSITY",
            "check_id": "CHECK-DEPLOYMENT-GRAPH-DIVERSITY-V1",
            "status": "unmet" if graph_repetition else "satisfied",
            "evidence_event_ids": evidence["graph"] or evidence["terminal"] or evidence["duration"],
            "detail": (
                f"observed repeated deployment graph in declared sample (count={graphs_metric["count"]}, unique={graphs_metric["unique_count"]}); limited-sample evidence requires retune"
                if graph_repetition
                else f"deployment graph diversity meets sample threshold (count={graphs_metric["count"]}, unique={graphs_metric["unique_count"]})"
            ),
        })

    counts = {status: sum(item["status"] == status for item in requirements)
              for status in ("satisfied", "unmet", "not-evaluable")}

    retune_required = (
        "DEFENSE_POLICY_REPETITION" in flags
        or "DEPLOYMENT_GRAPH_REPETITION" in flags
        or "BLUE_BRIEF_REPETITION" in flags
        or "CREDIT_PRESSURE" in flags
        or "MATCH_TOO_SHORT" in flags
        or "MISSING_TERMINAL" in flags
        or counts["unmet"] > 0
    )

    return {
        "match": {
            "duration_seconds": int(duration) if duration.is_integer() else duration,
            "action_count": len(actions),
            "actions_per_minute": action_rate,
        },
        "briefs": briefs,
        "defenses": {
            "policies": policies_metric,
            "graphs": graphs_metric,
        },
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
        "retune_required": retune_required,
        "verdict": "RETUNE_REQUIRED" if retune_required else "PASS",
    }


def diagnose_calibration_sample(
    sample: Sequence[Iterable[Mapping[str, Any]] | Mapping[str, Any]],
    *,
    config: Mapping[str, Any] | None = None,
    provider_credit_allowance: int | float | None = None,
    minimum_simulated_duration_seconds: int | float | None = None,
) -> dict[str, Any]:
    """Diagnose defense policy, graph, brief, and budget diversity across a declared sample of matches."""
    all_events: list[Mapping[str, Any]] = []
    all_briefs: list[str] = []
    all_policies: list[str] = []
    all_graphs: list[str] = []

    for entry in sample:
        if isinstance(entry, Mapping) and "events" in entry and isinstance(entry["events"], Iterable):
            match_events = _events(entry["events"])
        elif isinstance(entry, Mapping) and "telemetry" in entry and isinstance(entry["telemetry"], Iterable):
            match_events = _events(entry["telemetry"])
        elif isinstance(entry, Iterable) and not isinstance(entry, (str, bytes, Mapping)):
            match_events = _events(entry)
        elif isinstance(entry, Mapping):
            match_events = [entry]
        else:
            match_events = []

        all_events.extend(match_events)

        if isinstance(entry, Mapping) and "blue_brief" in entry:
            brief_val = entry["blue_brief"]
            if isinstance(brief_val, Mapping) and "family" in brief_val:
                all_briefs.append(str(brief_val["family"]))
            elif isinstance(brief_val, str):
                all_briefs.append(brief_val)
        elif isinstance(entry, Mapping) and "blue_briefs" in entry and isinstance(entry["blue_briefs"], Sequence):
            all_briefs.extend(str(b) for b in entry["blue_briefs"])

        if isinstance(entry, Mapping) and "defenses" in entry and isinstance(entry["defenses"], Mapping):
            for def_val in entry["defenses"].values():
                if isinstance(def_val, Mapping):
                    if "protected_policy" in def_val:
                        all_policies.append(str(def_val["protected_policy"]))
                    if "graph_hash" in def_val:
                        all_graphs.append(str(def_val["graph_hash"]))

        for event in match_events:
            if event.get("event") in {"blue_brief", "blue_brief_selected"} and "brief" in event:
                all_briefs.append(str(event["brief"]))
            if (event.get("event") == "deployment_promoted" or event.get("kind") == "deployment_promoted"):
                if "protected_policy" in event:
                    all_policies.append(str(event["protected_policy"]))
                if "graph_hash" in event:
                    all_graphs.append(str(event["graph_hash"]))

    result = diagnose_dry_run(
        all_events,
        blue_briefs=all_briefs,
        defense_policies=all_policies,
        deployment_graphs=all_graphs,
        config=config,
        provider_credit_allowance=provider_credit_allowance,
        minimum_simulated_duration_seconds=minimum_simulated_duration_seconds,
    )
    result["sample_size"] = len(sample)
    return result


calibrate_match = diagnose_dry_run
