"""Immutable, redacted evidence bundles derived from closed Match Telemetry."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import re
from typing import Any, Mapping, Sequence


def _digest(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class EvidenceFreezeError(ValueError):
    """Raised when material evidence cannot support an immutable freeze."""


def verify_evidence_bundle(bundle: Any | Mapping[str, Any]) -> dict[str, Any]:
    """Authenticate an evidence bundle at the #33 public verification seam.

    This is intentionally stricter than shape validation.  A report consumer
    must prove every digest, signature, version URL, provenance edge and the
    public score/outcome against the normalized hash-chained telemetry before
    treating a bundle as frozen evidence.
    """
    value = bundle.to_dict() if isinstance(bundle, EvidenceBundle) else dict(bundle)
    required = {"schema_version", "version", "url", "previous_version_url", "previous_bundle_hash", "provenance", "public", "restricted", "checksums", "bundle_hash", "signature"}
    if value.get("schema_version") != "sandboxer.evidence-bundle.v1" or required - value.keys():
        raise EvidenceFreezeError("EVIDENCE_BUNDLE_INVALID")
    version = value["version"]
    public, restricted, checksums = value["public"], value["restricted"], value["checksums"]
    if not isinstance(version, int) or version < 1 or not isinstance(public, dict) or not isinstance(restricted, dict) or not isinstance(checksums, dict):
        raise EvidenceFreezeError("EVIDENCE_BUNDLE_INVALID")
    series_id = public.get("specification", {}).get("series_id") if isinstance(public.get("specification"), dict) else None
    expected_url = f"sandboxer://evidence/{series_id}/v{version}"
    if not series_id or value["url"] != expected_url:
        raise EvidenceFreezeError("EVIDENCE_VERSION_URL_INVALID")
    expected_previous_url = None if version == 1 else f"sandboxer://evidence/{series_id}/v{version - 1}"
    if value["previous_version_url"] != expected_previous_url or (version == 1 and value["previous_bundle_hash"] is not None):
        raise EvidenceFreezeError("EVIDENCE_VERSION_PROVENANCE_INVALID")
    if version > 1 and (not isinstance(value["previous_bundle_hash"], str) or len(value["previous_bundle_hash"]) != 64):
        raise EvidenceFreezeError("EVIDENCE_VERSION_PROVENANCE_INVALID")
    provenance = tuple(value["provenance"])
    expected_provenance = tuple(f"sandboxer://evidence/{series_id}/v{number}" for number in range(1, version + 1))
    if provenance != expected_provenance:
        raise EvidenceFreezeError("EVIDENCE_VERSION_PROVENANCE_INVALID")
    if checksums != {"public": _digest(public), "restricted": _digest(restricted), "telemetry": checksums.get("telemetry")}:
        raise EvidenceFreezeError("EVIDENCE_CHECKSUM_INVALID")
    telemetry = public.get("normalized_telemetry")
    if not isinstance(telemetry, (list, tuple)):
        raise EvidenceFreezeError("MATERIAL_EVIDENCE_MISSING")
    # Normalized events retain the event envelope and hash chain; they are the
    # only telemetry a report may consume.
    _verify_chain(tuple(dict(event) for event in telemetry))
    sealed = tuple(restricted.get("sealed_event_hashes", ()))
    if len(sealed) != len(telemetry) or any(not isinstance(item, str) or len(item) != 64 for item in sealed):
        raise EvidenceFreezeError("EVIDENCE_TELEMETRY_PROOF_INVALID")
    if tuple(event.get("sealed_raw_event_hash") for event in telemetry) != sealed:
        raise EvidenceFreezeError("EVIDENCE_PUBLIC_RAW_BINDING_INVALID")
    bindings = tuple(restricted.get("public_event_bindings", ()))
    if bindings != tuple(_public_binding(event) for event in telemetry):
        raise EvidenceFreezeError("EVIDENCE_PUBLIC_RAW_BINDING_INVALID")
    # The raw telemetry is deliberately not available to public report code;
    # its authoritative checksum is sealed alongside the raw event hashes.
    if checksums.get("telemetry") != restricted.get("telemetry_checksum"):
        raise EvidenceFreezeError("EVIDENCE_TELEMETRY_CHECKSUM_INVALID")
    unsigned = {"version": version, "url": value["url"], "previous_version_url": value["previous_version_url"], "previous_bundle_hash": value["previous_bundle_hash"], "provenance": provenance, "checksums": checksums}
    if value["bundle_hash"] != _digest(unsigned):
        raise EvidenceFreezeError("EVIDENCE_BUNDLE_HASH_INVALID")
    verdict = public.get("auditor_verdict")
    if not isinstance(verdict, dict) or not verdict.get("valid") or not verdict.get("signed") or not verdict.get("signature"):
        raise EvidenceFreezeError("AUDITOR_VERDICT_INVALID")
    if value["signature"] != _digest({"bundle_hash": value["bundle_hash"], "verdict_signature": verdict["signature"]}):
        raise EvidenceFreezeError("EVIDENCE_SIGNATURE_INVALID")
    finished = {event["match_number"]: event for event in telemetry if event.get("event_type") == "MATCH_FINISHED" and isinstance(event.get("match_number"), int)}
    score = public.get("score_proof")
    if not isinstance(score, (tuple, list)) or len(score) != len(finished) or any(not isinstance(item, dict) or item.get("match_number") not in finished or item.get("winner") != finished[item["match_number"]].get("winner") or item.get("reason_code") != finished[item["match_number"]].get("reason_code") for item in score):
        raise EvidenceFreezeError("EVIDENCE_SCORE_PROOF_INVALID")
    terminal = next((event for event in reversed(telemetry) if event.get("event_type") == "SERIES_COMPLETED"), None)
    if not isinstance(terminal, dict) or terminal.get("winner") not in {item.get("winner") for item in score}:
        raise EvidenceFreezeError("EVIDENCE_OUTCOME_INVALID")
    return value


_SENSITIVE_KEY = re.compile(r"(?:flag|secret|credential|password|token|api[_-]?key|private[_-]?reasoning|internal[_-]?(?:path|endpoint))", re.I)
_CREDENTIAL_VALUE = re.compile(r"(?:flag\s*\{|(?:sk|pk|ghp|xox[baprs])-[A-Za-z0-9_-]{8,}|Bearer\s+[A-Za-z0-9._-]{8,})", re.I)


def _public(event: dict[str, Any], *, bind_raw: bool = False) -> dict[str, Any]:
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
    # Bound before redaction, so a public normalized event can be authenticated
    # against exactly one restricted raw-event digest without exposing content.
    if bind_raw:
        result["sealed_raw_event_hash"] = event["event_hash"]
    return result


def _rehash_public(events: Sequence[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    """Give the irreversible public projection its own verifiable hash chain."""
    previous = "0" * 64
    normalized: list[dict[str, Any]] = []
    for raw in events:
        event = {key: value for key, value in raw.items() if key != "event_hash"}
        event["previous_event_hash"] = previous
        event["event_hash"] = _digest(event)
        normalized.append(event)
        previous = event["event_hash"]
    return tuple(normalized)


def _public_binding(event: Mapping[str, Any]) -> str:
    projection = {key: value for key, value in event.items() if key not in {"event_hash", "previous_event_hash"}}
    return _digest({"sealed_raw_event_hash": event["sealed_raw_event_hash"], "public_projection": projection})


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
    public_events = _rehash_public(tuple(_public(dict(event), bind_raw=True) for event in telemetry))
    telemetry_checksum = _digest(tuple(telemetry))
    restricted = {
        "schema_version": "sandboxer.restricted-evidence.v1",
        "sealed_event_hashes": tuple(event["event_hash"] for event in telemetry),
        "public_event_bindings": tuple(_public_binding(event) for event in public_events),
        "telemetry_checksum": telemetry_checksum,
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
    checksums = {"public": _digest(public), "restricted": _digest(restricted), "telemetry": telemetry_checksum}
    url = f"sandboxer://evidence/{spec.series_id}/v{version}"
    provenance = (() if previous is None else previous.provenance) + (url,)
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

    def correction_index(self) -> dict[str, dict[str, Any]]:
        """Immutable-version graph for public correction navigation."""
        return {
            bundle.url: {
                "status": "current" if index == len(self._versions) - 1 else "superseded",
                "superseded_by": self._versions[index + 1].url if index + 1 < len(self._versions) else None,
                "previous": bundle.previous_version_url,
                "bundle_hash": bundle.bundle_hash,
            }
            for index, bundle in enumerate(self._versions)
        }
