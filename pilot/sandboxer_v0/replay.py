"""Deterministic, publication-safe terminal Replay projection.

Replay consumes a frozen evidence-bundle *version*.  It intentionally has no
Runner, filesystem, provider, or screen-capture port: the bundle's public
normalized telemetry is its only source of event data.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re
from typing import Any, Mapping


class ReplayError(ValueError):
    """Raised when a Replay cannot be built from a frozen evidence version."""


_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\)|[@-Z\\-_])")
_URL = re.compile(r"(?:https?|ftp)://[^\s<>'\"]+", re.IGNORECASE)
_UNSAFE_URI = re.compile(r"(?:javascript|data|file|ssh)://?[^\s<>'\"]+", re.IGNORECASE)
_PRIVATE_DOMAIN = re.compile(
    r"(?<![A-Za-z0-9.-])(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"(?:internal|local)(?![A-Za-z0-9.-])|(?<![A-Za-z0-9.-])localhost(?![A-Za-z0-9.-])",
    re.IGNORECASE,
)
_PRIVATE_REASONING = re.compile(
    r"(?is)(?:private\s+reasoning|chain[- ]of[- ]thought|internal\s+reasoning|thought\s+process)\s*[:=].*"
)
_CREDENTIAL = re.compile(
    r"(?ix)(?:bearer\s+|basic\s+)[A-Za-z0-9._~+/=-]{8,}|"
    r"\b(?:sk|pk|ghp|gho|github_pat|xox[baprs])-[A-Za-z0-9_-]{8,}\b|"
    r"\b(?:AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{20,}|npm_[A-Za-z0-9]{20,})\b|"
    r"\b(?:api[_-]?key|access[_-]?token|secret|password)\s*[:=]\s*[^\s,;]+"
)
_FLAG = re.compile(r"(?i)\bflag\s*[{:=(]\s*[^}\s,)]+\}?")
_PATH = re.compile(r"(?<![A-Za-z0-9])(?:/(?:home|root|tmp|proc|sys|etc|workspace|workspaces|private)/[^\s]+|[A-Za-z]:\\[^\s]+)")
_PRIVATE_IP = re.compile(
    r"(?<![\d.])(?:10\.(?:\d{1,3}\.){2}\d{1,3}|192\.168\.(?:\d{1,3}\.)\d{1,3}|"
    r"172\.(?:1[6-9]|2\d|3[0-1])\.(?:\d{1,3}\.)\d{1,3}|127\.(?:\d{1,3}\.){2}\d{1,3}|"
    r"169\.254\.(?:\d{1,3}\.)\d{1,3})(?![\d.])"
)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ALIAS = re.compile(r"^(?:alpha|beta)$", re.IGNORECASE)


def _digest(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def sanitize_terminal_text(value: object) -> str:
    """Normalize terminal bytes and irreversibly remove publication hazards."""
    text = str(value) if value is not None else ""
    text = _ANSI.sub("", text)
    text = _PRIVATE_REASONING.sub("[redacted reasoning]", text)
    text = re.sub(r"(?i)\b(?:private\s+reasoning|chain[- ]of[- ]thought|internal\s+reasoning|thought\s+process)\b", "[redacted reasoning]", text)
    text = _UNSAFE_URI.sub("[link removed]", text)
    text = _URL.sub("[link removed]", text)
    text = _CREDENTIAL.sub("[credential removed]", text)
    text = _FLAG.sub("[synthetic flag removed]", text)
    text = _PATH.sub("[internal path removed]", text)
    text = _PRIVATE_IP.sub("[private network removed]", text)
    text = _PRIVATE_DOMAIN.sub("[private network removed]", text)
    text = _CONTROL.sub("", text)
    return text


def _safe_label(value: object, fallback: str) -> str:
    label = sanitize_terminal_text(value).strip()
    label = re.sub(r"\s+", " ", label)
    if not label or label.startswith("[") or _ALIAS.fullmatch(label):
        return fallback
    return label[:160]


def _safe_source_url(value: object) -> str:
    """Keep only the bundle locator shape; never echo arbitrary URL metadata."""
    candidate = sanitize_terminal_text(value).strip()
    if re.fullmatch(r"sandboxer://evidence/[A-Za-z0-9._-]+/v[1-9][0-9]*", candidate):
        return candidate
    return "sandboxer://evidence/frozen"


def _frozen(bundle: object) -> Mapping[str, Any]:
    if hasattr(bundle, "to_dict"):
        bundle = bundle.to_dict()  # EvidenceBundle's public serialization seam.
    if not isinstance(bundle, Mapping) or bundle.get("schema_version") != "sandboxer.evidence-bundle.v1":
        raise ReplayError("FROZEN_EVIDENCE_REQUIRED")
    required = {"version", "url", "bundle_hash", "public", "restricted", "checksums", "signature"}
    if not required <= bundle.keys() or not isinstance(bundle["version"], int) or bundle["version"] < 1:
        raise ReplayError("FROZEN_EVIDENCE_VERSION_INVALID")
    public = bundle["public"]
    if not isinstance(public, Mapping) or public.get("schema_version") != "sandboxer.evidence-bundle.v1":
        raise ReplayError("FROZEN_EVIDENCE_PUBLIC_PROJECTION_INVALID")
    telemetry = public.get("normalized_telemetry")
    if not isinstance(telemetry, (list, tuple)):
        raise ReplayError("FROZEN_EVIDENCE_TELEMETRY_MISSING")
    restricted = bundle["restricted"]
    if not isinstance(restricted, Mapping) or restricted.get("redaction_irreversible") is not True:
        raise ReplayError("FROZEN_EVIDENCE_REDACTION_INVALID")
    if not isinstance(bundle["checksums"], Mapping) or not bundle.get("signature"):
        raise ReplayError("FROZEN_EVIDENCE_SIGNATURE_MISSING")
    if bundle["checksums"].get("public") != _digest(public):
        raise ReplayError("FROZEN_EVIDENCE_CHECKSUM_INVALID")
    return bundle


@dataclass(frozen=True)
class ReplayRenderer:
    """Stable renderer inputs; values are identifiers, never local paths."""

    renderer_version: str = "terminal-renderer.v1"
    asset_version: str = "assets.v1"
    font_version: str = "fonts.v1"
    reduced_motion: bool = True

    def descriptor(self) -> dict[str, Any]:
        return {
            "renderer_version": _safe_label(self.renderer_version, "terminal-renderer.v1"),
            "asset_version": _safe_label(self.asset_version, "assets.v1"),
            "font_version": _safe_label(self.font_version, "fonts.v1"),
            "reduced_motion": {
                "enabled": self.reduced_motion,
                "transitions": "none" if self.reduced_motion else "linear",
                "cursor_blink": False if self.reduced_motion else True,
                "scroll": "discrete" if self.reduced_motion else "real-time",
            },
        }


def _contrast(foreground: str, background: str) -> float:
    def channel(value: str) -> float:
        number = int(value, 16) / 255
        return number / 12.92 if number <= 0.04045 else ((number + 0.055) / 1.055) ** 2.4

    def luminance(color: str) -> float:
        rgb = [channel(color[index : index + 2]) for index in (1, 3, 5)]
        return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]

    foreground_luminance = luminance(foreground)
    background_luminance = luminance(background)
    lighter, darker = max(foreground_luminance, background_luminance), min(foreground_luminance, background_luminance)
    return (lighter + 0.05) / (darker + 0.05)


def _gradient(identity: str) -> dict[str, Any]:
    digest = _digest(identity)
    first = "#%02x%02x%02x" % (18 + int(digest[0:2], 16) % 22, 18 + int(digest[2:4], 16) % 22, 25 + int(digest[4:6], 16) % 25)
    second = "#%02x%02x%02x" % (30 + int(digest[6:8], 16) % 25, 24 + int(digest[8:10], 16) % 25, 32 + int(digest[10:12], 16) % 25)
    return {"type": "dark-gradient", "model_derived": True, "start": first, "end": second}


def _identities(specification: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    competitors = specification.get("competitors")
    if not isinstance(competitors, (list, tuple)) or len(competitors) != 2:
        raise ReplayError("PUBLIC_COMPETITOR_IDENTITIES_MISSING")
    identities: list[tuple[str, str]] = []
    for index, competitor in enumerate(competitors, start=1):
        if not isinstance(competitor, Mapping):
            raise ReplayError("PUBLIC_COMPETITOR_IDENTITIES_INVALID")
        public_name = competitor.get("public_name")
        adapter = competitor.get("adapter")
        model_id = adapter.get("model_id") if isinstance(adapter, Mapping) else None
        identity = _safe_label(public_name, "")
        # Alpha/Beta are internal role aliases.  If they appear in the frozen
        # source, use the declared provider model identity instead.
        if not identity or _ALIAS.fullmatch(str(public_name or "")):
            identity = _safe_label(model_id, f"Model {index}")
        if _ALIAS.fullmatch(identity):
            identity = f"Model {index}"
        identities.append((str(public_name or f"Model {index}"), identity))
    if identities[0][1] == identities[1][1]:
        raise ReplayError("PUBLIC_COMPETITOR_IDENTITIES_NOT_UNIQUE")
    return tuple(identities)


def _event_text(event: Mapping[str, Any]) -> str:
    for key in ("terminal_text", "text", "message", "output"):
        if key in event:
            return sanitize_terminal_text(event[key])
    # A frozen public response is represented by a one-way proof.  Showing the
    # proof itself adds no terminal evidence and risks confusing it with text.
    if "response_proof" in event:
        return "[model response redacted]"
    return ""


def build_replay(
    evidence_bundle: object,
    *,
    renderer_version: str = "terminal-renderer.v1",
    asset_version: str = "assets.v1",
    font_version: str = "fonts.v1",
    reduced_motion: bool = True,
) -> dict[str, Any]:
    """Build a deterministic Replay solely from one frozen evidence version."""
    bundle = _frozen(evidence_bundle)
    public = bundle["public"]
    identities = _identities(public.get("specification", {}))
    aliases = {alias: identity for alias, identity in identities}
    by_identity = {identity: index for index, (_, identity) in enumerate(identities)}
    telemetry = public["normalized_telemetry"]

    panes = []
    for index, (_, identity) in enumerate(identities):
        gradient = _gradient(identity)
        panes.append({
            "index": index,
            "identity": identity,
            "identity_cue": f"MODEL: {identity}",
            "background": gradient,
            "terminal_text": "#f0eee7",
            "contrast_ratio": min(_contrast("#f0eee7", gradient["start"]), _contrast("#f0eee7", gradient["end"])),
            "safe_zone": {"top": 0.06, "bottom": 0.08, "left": 0.03, "right": 0.03},
        })

    frames: list[dict[str, Any]] = []
    scroll = [0, 0]
    previous_time = -1
    for sequence, raw_event in enumerate(telemetry, start=1):
        if not isinstance(raw_event, Mapping):
            raise ReplayError("FROZEN_EVIDENCE_TELEMETRY_INVALID")
        monotonic = raw_event.get("orchestrator_monotonic_ns")
        if not isinstance(monotonic, int) or monotonic < previous_time:
            raise ReplayError("AUTHORITATIVE_TIMING_INVALID")
        previous_time = monotonic
        alias = str(raw_event.get("competitor", ""))
        identity = aliases.get(alias)
        pane_index = by_identity.get(identity, 0) if identity else None
        text = _event_text(raw_event)
        if pane_index is not None and text:
            scroll[pane_index] += 1
        frame = {
            "sequence": sequence,
            "at_monotonic_ns": monotonic,
            "at_wall_time_utc": _safe_label(raw_event.get("wall_time_utc"), "unknown-time"),
            "event_id": _safe_label(raw_event.get("event_id"), f"event-{sequence}"),
            "event_type": _safe_label(raw_event.get("event_type"), "telemetry-event"),
            "phase": _safe_label(raw_event.get("phase"), "unspecified"),
            "turn": raw_event.get("turn") if isinstance(raw_event.get("turn"), int) else None,
            "pane": pane_index,
            "text": text,
            "scroll": {"left": scroll[0], "right": scroll[1]},
        }
        frames.append(frame)

    frame_samples = tuple({"sequence": frame["sequence"], "frame_hash": _digest(frame)} for frame in frames)
    renderer = ReplayRenderer(renderer_version, asset_version, font_version, reduced_motion)
    replay = {
        "schema_version": "sandboxer.replay.v1",
        "source_evidence": {"url": _safe_source_url(bundle["url"]), "version": bundle["version"]},
        "source_bundle_hash": str(bundle["bundle_hash"]) if re.fullmatch(r"[0-9a-f]{64}", str(bundle["bundle_hash"])) else _digest(bundle["bundle_hash"]),
        "renderer": renderer.descriptor(),
        "layout": {
            "split": {"left": 0.5, "right": 0.5, "permanent": True},
            "safe_zones": {"top": 0.06, "bottom": 0.08, "left": 0.03, "right": 0.03},
            "scroll_mode": "real-time-authoritative",
            "simultaneous_events": "preserve-source-order",
        },
        "panes": tuple(panes),
        "frames": tuple(frames),
        "frame_samples": frame_samples,
    }
    return replay


# Friendly explicit alias for callers that prefer the noun used by the domain.
render_replay = build_replay


__all__ = ["ReplayError", "ReplayRenderer", "build_replay", "render_replay", "sanitize_terminal_text"]
