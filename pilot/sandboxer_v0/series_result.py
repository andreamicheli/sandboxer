"""Best-of-3 series aggregation for broadcast episodes.

A series driver (``scripts/run_series.py``) runs three sequential Command
Code matches, each with its own match id suffix (``-m1``/``-m2``/``-m3``)
and evidence files.  This module turns those per-match ``result.json``
payloads into one deterministic, human-readable series record:

  - per-match winners (public identities) and reason codes,
  - the series winner and the decision basis.

Series decision contract (documented, deterministic):

  1. First to two match wins takes the series (``first_to_two_wins``).
  2. With no side reaching two wins (e.g. a 1-1-1 win-win-draw split by
     reason codes), the tie is broken by the lowest total provider token
     consumption across all three matches (``lowest_total_tokens_tiebreak``):
     input + output + cache-read + cache-write tokens summed over every
     recorded usage entry (blue/interview/red phases).
  3. If token totals are also exactly tied, the series stays unresolved
     (``unresolved_tie``, winner ``None``) instead of picking arbitrarily.

All functions are pure so the decision logic can be unit-tested without
running any match.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .artifact_converter import _public_identity

SERIES_SCHEMA = "sandboxer.series-result.v1"
SERIES_PROTOCOL = "best_of_3"
WINS_TO_CLINCH = 2

BASIS_FIRST_TO_TWO = "first_to_two_wins"
BASIS_TOKEN_TIEBREAK = "lowest_total_tokens_tiebreak"
BASIS_UNRESOLVED = "unresolved_tie"

_TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")


def _int(value: Any) -> int:
    """Best-effort non-negative int coercion; junk counts as nothing."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


def identity_total_tokens(result: Mapping[str, Any], identity: str) -> int:
    """Total provider tokens one identity spent across one whole match.

    Sums input/output/cache tokens over every usage entry in every phase
    whose observed model maps to ``identity``.  A failed match without a
    usage section simply totals zero.
    """
    usage = result.get("usage")
    if not isinstance(usage, Mapping):
        return 0
    total = 0
    for phase_entries in usage.values():
        if isinstance(phase_entries, Mapping) or not phase_entries:
            continue
        for entry in phase_entries:
            if not isinstance(entry, Mapping):
                continue
            if identity and _public_identity(str(entry.get("model", ""))) != identity:
                continue
            total += sum(_int(entry.get(field)) for field in _TOKEN_FIELDS)
    return total


def decide_series_winner(
    results: Sequence[Mapping[str, Any]],
    identities: Sequence[str],
) -> dict[str, Any]:
    """Decide the series winner from per-match result payloads.

    Returns ``{"wins": {identity: wins}, "series_winner": identity|None,
    "decision_basis": basis, "token_totals": {identity: tokens}|None}``.
    """
    wins: dict[str, int] = {}
    for result in results:
        for model in result.get("models") or ():
            wins.setdefault(_public_identity(str(model)), 0)
    for name in identities:
        wins.setdefault(name, 0)

    for result in results:
        raw_winner = result.get("winner")
        if not raw_winner:
            continue  # draw / failed match: no win awarded
        name = _public_identity(str(raw_winner))
        wins[name] = wins.get(name, 0) + 1

    clinched = [name for name, count in wins.items() if count >= WINS_TO_CLINCH]
    if len(clinched) == 1:
        return {"wins": wins, "series_winner": clinched[0],
                "decision_basis": BASIS_FIRST_TO_TWO, "token_totals": None}

    # No side reached two wins (a drawn match absorbed it): token tie-break.
    token_totals = {name: sum(identity_total_tokens(result, name) for result in results)
                    for name in sorted(wins)}
    lowest = min(token_totals.values(), default=0)
    cheapest = [name for name, total in token_totals.items() if total == lowest]
    if len(cheapest) == 1:
        return {"wins": wins, "series_winner": cheapest[0],
                "decision_basis": BASIS_TOKEN_TIEBREAK, "token_totals": token_totals}
    return {"wins": wins, "series_winner": None,
            "decision_basis": BASIS_UNRESOLVED, "token_totals": token_totals}


def build_series_summary(
    series_id: str,
    results: Sequence[Mapping[str, Any]],
    *,
    match_ids: Sequence[str] | None = None,
    evidence_paths: Sequence[Mapping[str, str]] | None = None,
) -> dict[str, Any]:
    """Build the ``<series-id>.series.json`` payload from match results.

    ``results`` are the per-match ``result.json`` payloads in play order.
    ``match_ids`` optionally records each match's id (defaults to each
    payload's own ``match_id``); ``evidence_paths`` optionally attaches the
    declared telemetry/result file paths per match.
    """
    identities: list[str] = []
    for result in results:
        for model in result.get("models") or ():
            name = _public_identity(str(model))
            if name not in identities:
                identities.append(name)

    decision = decide_series_winner(results, identities)
    matches: list[dict[str, Any]] = []
    for index, result in enumerate(results, start=1):
        raw_winner = result.get("winner")
        entry: dict[str, Any] = {
            "match_number": index,
            "match_id": str((match_ids[index - 1] if match_ids and index <= len(match_ids)
                             else result.get("match_id", ""))),
            "winner": _public_identity(str(raw_winner)) if raw_winner else None,
            "outcome": str(result.get("outcome", "")),
            "reason_code": str(result.get("reason_code", "")),
            "captures": list(result.get("captures", []) or []),
            "result_status": str(result.get("result", "passed")),
        }
        if evidence_paths and index <= len(evidence_paths):
            entry["evidence"] = dict(evidence_paths[index - 1])
        attempts = result.get("attempts")
        if isinstance(attempts, list) and attempts:
            entry["attempts"] = [dict(attempt) for attempt in attempts
                                 if isinstance(attempt, Mapping)]
        matches.append(entry)

    summary = {
        "schema_version": SERIES_SCHEMA,
        "series_id": series_id,
        "protocol": SERIES_PROTOCOL,
        "matches_to_play": len(results),
        "identities": identities,
        "matches": matches,
        "wins": decision["wins"],
        "series_winner": decision["series_winner"],
        "decision_basis": decision["decision_basis"],
    }
    brief_families: list[str] = []
    for result in results:
        manifest = result.get("blue_brief")
        if isinstance(manifest, Mapping):
            brief_families.append(str(manifest.get("family", "")))
    if (len(results) > 1 and len(brief_families) == len(results)
            and len(set(brief_families)) == 1):
        summary["brief_variety"] = "low"
    if decision["token_totals"] is not None:
        summary["tie_break"] = {
            "rule": ("lowest total provider tokens (input+output+cache) across "
                     "the three matches; applied when no side reaches two wins"),
            "token_totals": decision["token_totals"],
        }
    return summary


def series_report_outcome(summary: Mapping[str, Any]) -> dict[str, str]:
    """The report-layer outcome block a series recap should display."""
    winner = str(summary.get("series_winner") or "")
    basis = str(summary.get("decision_basis") or "")
    wins = summary.get("wins") or {}
    standings = ", ".join(f"{name} {count}" for name, count in sorted(wins.items()))
    basis_text = {
        BASIS_FIRST_TO_TWO: "first to two match wins",
        BASIS_TOKEN_TIEBREAK: "tie-break on lowest total tokens across the series",
        BASIS_UNRESOLVED: "series unresolved after tie-breaks",
    }.get(basis, basis)
    return {"winner": winner, "basis": f"Best-of-3 series ({standings}). Decided by {basis_text}."}
