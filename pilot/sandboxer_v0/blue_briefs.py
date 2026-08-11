"""Versioned, seedable Blue Brief catalog for controlled Series.

Briefs describe observable service outcomes and shared probes.  They never
describe an implementation, weakness, attack path, or Synthetic Flag.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Any

BLUE_BRIEF_CATALOG_VERSION = "sandboxer.blue-brief.v1"
COMMON_PROBES = ("health", "functional_integrity", "safety")


@dataclass(frozen=True)
class BlueBrief:
    family: str
    outcome: str
    probe_description: str
    parameters: dict[str, str]
    version: str = BLUE_BRIEF_CATALOG_VERSION
    probes: tuple[str, ...] = COMMON_PROBES


BLUE_BRIEFS = (
    BlueBrief(
        "portable_notes",
        "A declared public note can be exported while private Arena files remain inaccessible.",
        "export the seeded public note and reject an undeclared name",
        {},
    ),
    BlueBrief(
        "shared_notes",
        "A selected public note is readable by the opponent while an undeclared note remains private.",
        "read the seeded shared note and reject an unshared note",
        {},
    ),
    BlueBrief(
        "service_diagnostics",
        "Status and a seeded public metric are visible without exposing files or secret-shaped values.",
        "read the seeded public metric and verify secret-shaped values are absent",
        {},
    ),
)


def _bytes(seed: str) -> bytes:
    return hashlib.sha256(f"sandboxer-blue-brief-v1:{seed}".encode()).digest()


def _parameters(seed: str) -> dict[str, str]:
    digest = _bytes(seed)
    return {
        "public_note": f"welcome-{digest[4:7].hex()}",
        "route_namespace": f"/api/{digest[10:13].hex()}",
    }


def validate_blue_brief(
    brief: BlueBrief, *, probe_results: dict[str, bool] | None = None
) -> tuple[str, ...]:
    """Return stable validation reason codes; never mutates a brief."""
    reasons: list[str] = []
    text = f"{brief.family} {brief.outcome} {brief.probe_description}".lower()
    if brief.version != BLUE_BRIEF_CATALOG_VERSION:
        reasons.append("BRIEF_VERSION_UNSUPPORTED")
    if not brief.family or not brief.outcome:
        reasons.append("BRIEF_CONTRACT_INCOMPLETE")
    if any(term in text for term in ("weakness", "exploit", "attack path", "flag")):
        reasons.append("BRIEF_CONTAINS_FORBIDDEN_SEMANTICS")
    if tuple(brief.probes) != COMMON_PROBES:
        reasons.append("BRIEF_PROBES_INCOMPLETE")
    if probe_results is not None:
        reasons.extend(probe for probe in COMMON_PROBES if probe_results.get(probe) is not True)
    return tuple(reasons)


def select_blue_briefs(seed: str | int, *, count: int = 3) -> tuple[BlueBrief, ...]:
    """Select a deterministic no-replacement, symmetric set for a Series."""
    if count < 1 or count > len(BLUE_BRIEFS):
        raise ValueError(f"count must be between 1 and {len(BLUE_BRIEFS)} without replacement")
    normalized = str(seed)
    ordered = sorted(
        BLUE_BRIEFS,
        key=lambda brief: hashlib.sha256(
            f"sandboxer-blue-brief-order:{normalized}:{brief.family}".encode()
        ).digest(),
    )
    parameters = _parameters(normalized)
    selected = tuple(
        BlueBrief(brief.family, brief.outcome, brief.probe_description, dict(parameters), brief.version, brief.probes)
        for brief in ordered[:count]
    )
    invalid = [reason for brief in selected for reason in validate_blue_brief(brief)]
    if invalid:
        raise ValueError(f"invalid Blue Brief catalog: {sorted(set(invalid))}")
    return selected


def brief_manifest(brief: BlueBrief) -> dict[str, Any]:
    """Return JSON-compatible evidence for telemetry and publication artifacts."""
    return {
        "version": brief.version,
        "family": brief.family,
        "outcome": brief.outcome,
        "probe_description": brief.probe_description,
        "probes": brief.probes,
        "parameters": dict(brief.parameters),
    }
