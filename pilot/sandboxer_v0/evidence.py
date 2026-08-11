"""Immutable, redacted evidence bundles derived from closed Match Telemetry."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import re
from typing import Any, Sequence


def _digest(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class EvidenceFreezeError(ValueError):
    """Raised when material evidence cannot support an immutable freeze."""


_SENSITIVE_KEY = re.compile(r"(?:flag|secret|credential|password|token|api[_-]?key|private[_-]?reasoning|internal[_-]?(?:path|endpoint))", re.I)
_CREDENTIAL_VALUE = re.compile(r"(?:flag\s*\{|(?:sk|pk|ghp|xox[baprs])-[A-Za-z0-9_-]{8,}|Bearer\s+[A-Za-z0-9._-]{8,})", re.I)


def _public(event: dict[str, Any]) -> dict[str, Any]:
    """Irreversible public redaction: sensitive values become one-way proofs."""
    result: dict[str, Any] = {}
    for key, value in event.items():
        if key in {"response", "prompt", "token_stream"} or _SENSITIVE_KEY.search(key):
            result[f"{key}_proof"] = _digest({"redaction": "sandboxer.public-redaction.v1", "value": value})
        elif isinstance(value, str) and _CREDENTIAL_VALUE.search(value):
            result[f"{key}_proof"] = _digest({"redaction": "sandboxer.public-redaction.v1", "value": value})
        elif isinstance(value, dict):
            result[key] = _public(value)
        elif isinstance(value, (list, tuple)):
            result[key] = tuple(_public(item) if isinstance(item, dict) else item for item in value)
        else:
            result[key] = value
    return result


def _verify_chain(events: Sequence[dict[str, Any]]) -> None:
    if not events:
        raise EvidenceFreezeError("MATERIAL_EVIDENCE_MISSING")
    required = {
        "schema_version", "event_id", "event_type", "evidence_kind", "wall_time_utc",
        "orchestrator_monotonic_ns", "idempotency_key", "component_versions",
        "redaction_class", "previous_event_hash", "event_hash",
    }
    previous = "0" * 64
    ids: set[str] = set()
    last_clock = -1
    for event in events:
        if missing := required - event.keys():
            raise EvidenceFreezeError(f"MATERIAL_EVIDENCE_MISSING:{','.join(sorted(missing))}")
        if event["event_id"] in ids or event["previous_event_hash"] != previous:
            raise EvidenceFreezeError("TELEMETRY_DISCONTINUITY")
        unhashed = {key: value for key, value in event.items() if key != "event_hash"}
        if _digest(unhashed) != event["event_hash"]:
            raise EvidenceFreezeError("TELEMETRY_HASH_INVALID")
        if event["orchestrator_monotonic_ns"] < last_clock:
            raise EvidenceFreezeError("TELEMETRY_DISCONTINUITY")
        ids.add(event["event_id"])
        previous = event["event_hash"]
        last_clock = event["orchestrator_monotonic_ns"]
    kinds = {event["evidence_kind"] for event in events}
    material = {"MODEL_CLAIMED", "RUNNER_OBSERVED", "ORCHESTRATOR_VERIFIED"}
    if not material <= kinds:
        raise EvidenceFreezeError("MATERIAL_EVIDENCE_ASYMMETRIC")


@dataclass(frozen=True)
class EvidenceBundle:
    schema_version: str
    version: int
    url: str
    previous_version_url: str | None
    previous_bundle_hash: str | None
    provenance: tuple[str, ...]
    public: dict[str, Any]
    restricted: dict[str, Any]
    checksums: dict[str, str]
    bundle_hash: str
    signature: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def freeze_evidence_bundle(
    *, spec: Any, telemetry: Sequence[dict[str, Any]], results: Sequence[dict[str, Any]],
    verdict: dict[str, Any], version: int = 1, previous: EvidenceBundle | None = None,
) -> EvidenceBundle:
    """Freeze only complete, independently checkable material evidence."""
    _verify_chain(telemetry)
    if not verdict.get("signed"):
        raise EvidenceFreezeError("AUDITOR_VERDICT_UNSIGNED")
    if version < 1 or (previous is None) != (version == 1):
        raise EvidenceFreezeError("EVIDENCE_VERSION_PROVENANCE_INVALID")
    public_events = tuple(_public(dict(event)) for event in telemetry)
    restricted = {
        "schema_version": "sandboxer.restricted-evidence.v1",
        "sealed_event_hashes": tuple(event["event_hash"] for event in telemetry),
        "seed_reveal_proof": _digest({"seed": spec.seed, "series": spec.series_id}),
        "health_score": {
            "healthy": sum(1 for event in telemetry if event.get("event_type") == "RUNNER_HEALTH" and event.get("healthy") is True),
            "observations": sum(1 for event in telemetry if event.get("event_type") == "RUNNER_HEALTH"),
        },
        "redaction_irreversible": True,
    }
    health = tuple(event["event_id"] for event in telemetry if event["event_type"] == "RUNNER_HEALTH")
    score = tuple({"match_number": item["match_number"], "winner": item.get("winner"), "reason_code": item["reason_code"]} for item in results)
    public = {
        "schema_version": "sandboxer.evidence-bundle.v1",
        "specification": asdict(spec),
        "contracts": {"telemetry": "sandboxer.match-telemetry.v1", "auditor": verdict.get("schema_version")},
        "seed_commitment": _digest({"seed": spec.seed, "series": spec.series_id}),
        "normalized_telemetry": public_events,
        "evidence_derivation": tuple({"event_id": event["event_id"], "kind": event["evidence_kind"]} for event in telemetry),
        "health_proof": health,
        "score_proof": score,
        "auditor_verdict": verdict,
    }
    checksums = {"public": _digest(public), "restricted": _digest(restricted), "telemetry": _digest(tuple(telemetry))}
    url = f"sandboxer://evidence/{spec.series_id}/v{version}"
    provenance = (() if previous is None else (*previous.provenance, previous.url)) + (url,)
    unsigned = {
        "version": version, "url": url, "previous_version_url": previous.url if previous else None,
        "previous_bundle_hash": previous.bundle_hash if previous else None, "provenance": provenance,
        "checksums": checksums,
    }
    bundle_hash = _digest(unsigned)
    return EvidenceBundle("sandboxer.evidence-bundle.v1", version, url, previous.url if previous else None,
        previous.bundle_hash if previous else None, provenance, public, restricted, checksums, bundle_hash,
        _digest({"bundle_hash": bundle_hash, "verdict_signature": verdict["signature"]}))


class EvidenceVersionStore:
    """Small deterministic correction ledger; old immutable versions remain addressable."""

    def __init__(self) -> None:
        self._versions: list[EvidenceBundle] = []

    @property
    def current(self) -> EvidenceBundle:
        if not self._versions:
            raise EvidenceFreezeError("NO_EVIDENCE_VERSION")
        return self._versions[-1]

    def freeze(self, **kwargs: Any) -> EvidenceBundle:
        previous = self._versions[-1] if self._versions else None
        bundle = freeze_evidence_bundle(version=len(self._versions) + 1, previous=previous, **kwargs)
        self._versions.append(bundle)
        return bundle
