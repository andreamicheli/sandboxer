"""Canonical telemetry-to-artifact conversion: an official pipeline phase.

Runtime match outputs (``<match>.telemetry.jsonl`` and ``<match>.result.json``
written by ``run_command_code_match.py``) use a different schema than the
frozen publication artifacts (evidence bundle, ``replay.json``,
``video-manifest.json``).  During the first real end-to-end run this gap was
bridged by an ad-hoc script (``scripts/build_real_artifacts.py``) whose
fallback path even dereferenced the frame list before it was defined.  This
module promotes that conversion into an official phase with an explicit
schema, deterministic derivation, written hashes and post-write validation.

Canonical artifacts:

  CanonicalEvidence  schema ``sandboxer.evidence.v1``: redacted match facts
                     (run_id, match_id, competitors, phases, events, outcome,
                     tool_calls, defenses, attacks)
  CanonicalReplay    schema ``sandboxer.replay.v1``: terminal frames derived
                     from the evidence events with deterministic timestamps

Redaction is structural: tool calls carry only the tool name, the allowed
verdict, a reason code and the classifier-safe terminal text.  Arguments,
responses and any other raw provider material are never copied.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, TypedDict

EVIDENCE_FILENAME = "evidence.json"
REPLAY_FILENAME = "replay.json"
HASHES_FILENAME = "artifacts.sha256"

EVIDENCE_SCHEMA = "sandboxer.evidence.v1"
REPLAY_SCHEMA = "sandboxer.replay.v1"

# Public names used by the broadcast layer (matches the synthetic fixtures and
# the video logo/accent registry).
IDENTITY_A = "Laguna S 2.1"     # poolside/laguna-s-2.1-free
IDENTITY_B = "Muse Spark 1.2"   # meta/muse-spark-1.2-contributor
_MODEL_TO_PANE = {
    "poolside/laguna-s-2.1-free": 0,
    "meta/muse-spark-1.2-contributor": 1,
}
_PANE_NAMES = {0: IDENTITY_A, 1: IDENTITY_B}

# Neutral terminal text shown for each allowed tool (TTS classifier-safe).
_TOOL_TEXT = {
    "inspect_service": "inspect: declared service surface",
    "deploy_service": "deploy: proposed service spec",
    "request_own_service": "verify: self-service health check",
    "finish_phase": "finish: phase wraps up",
    "describe_target_service": "target: opponent service contract",
    "http_request": "probe: HTTP request on target",
    "submit_flag": "verify: objective token submission",
}

# Red-phase tools that constitute offensive action, mapped to attack kinds.
_ATTACK_KINDS = {
    "describe_target_service": "target_recon",
    "http_request": "http_probe",
    "submit_flag": "flag_submission",
}


def _public_identity(model_id: str) -> str:
    return _PANE_NAMES.get(_MODEL_TO_PANE.get(model_id, -1), model_id)


class CanonicalEvent(TypedDict):
    """One extracted semantic event; replay frames derive from these 1:1."""

    event_id: str
    sequence: int
    event_type: str
    phase: str
    competitor: str | None
    pane: int | None
    at_monotonic_ns: int
    text: str


class CanonicalFrame(TypedDict):
    """One terminal replay frame with a deterministic timestamp."""

    sequence: int
    at_monotonic_ns: int
    event_id: str
    event_type: str
    phase: str
    pane: int | None
    text: str
    match_number: int


class CanonicalToolCall(TypedDict):
    """Redacted tool decision: no arguments, responses or raw output."""

    competitor: str | None
    pane: int | None
    phase: str
    tool: str
    allowed: bool
    reason_code: str | None
    text: str
    at_monotonic_ns: int


class CanonicalAttack(TypedDict):
    attacker: str | None
    kind: str
    tool: str
    at_monotonic_ns: int


@dataclass(frozen=True)
class CanonicalEvidence:
    """Redacted, structured facts of one match (schema sandboxer.evidence.v1)."""

    run_id: str
    match_id: str
    competitors: list[str]
    phases: list[str]
    events: list[CanonicalEvent]
    outcome: dict[str, Any]
    tool_calls: list[CanonicalToolCall]
    defenses: dict[str, dict[str, Any]]
    attacks: list[CanonicalAttack]
    schema_version: str = EVIDENCE_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "match_id": self.match_id,
            "competitors": list(self.competitors),
            "phases": list(self.phases),
            "events": [dict(event) for event in self.events],
            "outcome": deepcopy(self.outcome),
            "tool_calls": [dict(tool_call) for tool_call in self.tool_calls],
            "defenses": {name: dict(defense) for name, defense in self.defenses.items()},
            "attacks": [dict(attack) for attack in self.attacks],
        }


@dataclass(frozen=True)
class CanonicalReplay:
    """Deterministic terminal replay derived from the evidence events."""

    source_bundle_hash: str
    panes: list[dict[str, str]]
    layout: dict[str, Any]
    frames: list[CanonicalFrame]
    schema_version: str = REPLAY_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source_bundle_hash": self.source_bundle_hash,
            "panes": [dict(pane) for pane in self.panes],
            "layout": deepcopy(self.layout),
            "frames": [dict(frame) for frame in self.frames],
        }


def _frame_from_event(event: CanonicalEvent) -> CanonicalFrame:
    return {
        "sequence": event["sequence"],
        "at_monotonic_ns": event["at_monotonic_ns"],
        "event_id": event["event_id"],
        "event_type": event["event_type"],
        "phase": event["phase"],
        "pane": event["pane"],
        "text": event["text"],
        "match_number": 1,
    }


def _redacted_tool_call(event: Mapping[str, Any], *, model: str, pane: int | None,
                        phase: str, ns: int) -> CanonicalToolCall:
    tool = str(event.get("tool", ""))
    reason_code = event.get("reason_code")
    return {
        "competitor": _public_identity(model) if model else None,
        "pane": pane,
        "phase": phase,
        "tool": tool,
        "allowed": bool(event.get("allowed")),
        "reason_code": str(reason_code) if reason_code is not None else None,
        "text": _TOOL_TEXT.get(tool, f"tool: {tool}"),
        "at_monotonic_ns": int(ns),
    }


def convert(runtime_telemetry: Sequence[Mapping[str, Any]], result: Mapping[str, Any]) -> tuple[CanonicalEvidence, CanonicalReplay]:
    """Extract canonical evidence and replay from runtime telemetry + result.

    ``runtime_telemetry`` is the parsed telemetry JSONL (one mapping per line,
    as emitted by ``run_command_code_match.py``); ``result`` is the parsed
    ``result.json``.  The derivation is deterministic: identical inputs yield
    byte-identical artifacts.

    The minimal-fallback frame path appends to lists created before the
    telemetry walk; the historical ad-hoc implementation dereferenced the
    frame list before its definition on this very path.
    """
    telemetry = list(runtime_telemetry)
    start_ns = telemetry[0]["monotonic_ns"] if telemetry else 0
    events: list[CanonicalEvent] = []
    tool_calls: list[CanonicalToolCall] = []
    attacks: list[CanonicalAttack] = []
    sequence = 0

    def add(*, phase: str, competitor: str | None, pane: int | None,
            etype: str, text: str, ns: int) -> None:
        nonlocal sequence
        sequence += 1
        events.append({
            "event_id": f"e{sequence:02d}",
            "sequence": sequence,
            "event_type": etype,
            "phase": phase,
            "competitor": competitor,
            "pane": pane,
            "at_monotonic_ns": int(ns),
            "text": text,
        })

    for event in telemetry:
        kind = event.get("kind") or event.get("event_type")
        ns = event.get("monotonic_ns", start_ns)
        model = event.get("model") or event.get("competitor") or ""
        pane = _MODEL_TO_PANE.get(model) if model else None
        phase = str(event.get("phase", "blue"))

        if kind == "match_started":
            add(phase="blue", competitor=None, pane=0, etype="MATCH_STARTED",
                text="match started - two isolated services, one flag each", ns=ns)
            add(phase="blue", competitor=None, pane=1, etype="MATCH_STARTED",
                text="match started - services coming up", ns=ns + 1)
            continue
        if kind == "blue_finished":
            add(phase="blue", competitor=None, pane=0, etype="PHASE_TRANSITION",
                text="blue phase complete - defenses are live", ns=ns)
            continue
        if kind == "interview_finished":
            add(phase="blue", competitor=None, pane=1, etype="PHASE_TRANSITION",
                text="interview complete - rules confirmed", ns=ns)
            continue
        if kind == "interview_line":
            # Verbatim post-blue interview answer; unlike tool decisions the
            # line is kept even without a known pane so no interview is lost.
            add(phase="interview",
                competitor=_public_identity(str(model)) if model else None,
                pane=pane, etype="INTERVIEW_RECORDED",
                text=str(event.get("text", "")), ns=ns)
            continue
        if kind == "deployment_promoted":
            if pane is None:
                continue
            policy = str(event.get("protected_policy", "?")).upper()
            recovery = str(event.get("recovery_posture", "?")).upper()
            add(phase="blue", competitor=_public_identity(str(model)), pane=pane, etype="MODEL_RESPONSE",
                text=f"defense promoted: {policy} policy, {recovery} recovery", ns=ns)
            continue
        if kind == "tool_decision":
            if pane is None:
                continue
            tool_call = _redacted_tool_call(event, model=str(model), pane=pane, phase=phase, ns=ns)
            tool_calls.append(tool_call)
            add(phase=phase, competitor=tool_call["competitor"], pane=pane, etype="TOOL_CALL",
                text=tool_call["text"], ns=ns)
            if phase == "red" and tool_call["tool"] in _ATTACK_KINDS:
                attacks.append({
                    "attacker": tool_call["competitor"],
                    "kind": _ATTACK_KINDS[tool_call["tool"]],
                    "tool": tool_call["tool"],
                    "at_monotonic_ns": tool_call["at_monotonic_ns"],
                })
            continue
        if kind == "match_finished":
            winner = str(result.get("winner", ""))
            winner_name = _public_identity(winner)
            reason = str(result.get("reason_code", ""))
            add(phase="red", competitor=None, pane=0, etype="MATCH_FINISHED",
                text=f"match finished - {reason}, winner {winner_name}", ns=ns)
            continue
        if kind == "teardown":
            # A match that died at bootstrap (no gameplay) has no teardown
            # worth narrating: its event lands at the merged timeline end and
            # the TTS audio duration pushes the block past the recap boundary
            # (episode-v8d). Skip when the result records no winner.
            if result.get("winner"):
                add(phase="red", competitor=None, pane=1, etype="MATCH_FINISHED",
                    text="teardown complete - runners destroyed", ns=ns)
            continue
        # Skip the high-volume provider frames; tool decisions carry the story.
        continue

    # Ensure at least a minimal frame set even for unusual telemetry.  Both
    # lists above already exist here, so this path cannot hit an undefined name.
    if not events:
        add(phase="blue", competitor=IDENTITY_A, pane=0, etype="MATCH_STARTED",
            text="match started", ns=start_ns)
        add(phase="red", competitor=IDENTITY_B, pane=1, etype="MATCH_FINISHED",
            text="match finished", ns=start_ns + 1)

    frames = [_frame_from_event(event) for event in events]

    match_id = str(result.get("match_id") or result.get("seed") or "")
    winner_raw = result.get("winner")
    evidence = CanonicalEvidence(
        run_id=str(result.get("run_id") or match_id),
        match_id=match_id,
        competitors=[_public_identity(str(model)) for model in (result.get("models") or [])],
        phases=list(dict.fromkeys(event["phase"] for event in events)),
        events=events,
        outcome={
            "winner": _public_identity(str(winner_raw)) if winner_raw else "",
            "reason_code": str(result.get("reason_code", "")),
            "outcome": str(result.get("outcome", "")),
            "captures": list(result.get("captures", [])),
        },
        tool_calls=tool_calls,
        defenses={
            _public_identity(str(model)): {
                "protected_policy": (defense or {}).get("protected_policy", "?"),
                "recovery_posture": (defense or {}).get("recovery_posture", "?"),
            }
            for model, defense in (result.get("defenses") or {}).items()
        },
        attacks=attacks,
    )
    replay = CanonicalReplay(
        schema_version=REPLAY_SCHEMA,
        source_bundle_hash=str(result.get("seed", "match"))[:64].ljust(64, "0"),
        panes=[{"identity": IDENTITY_A}, {"identity": IDENTITY_B}],
        layout={"split": {"left": 0.5, "right": 0.5, "permanent": True}},
        frames=frames,
    )
    return evidence, replay


def write_artifacts(evidence: CanonicalEvidence, replay: CanonicalReplay,
                    out_dir: str | Path) -> dict[str, str]:
    """Write ``evidence.json`` and ``replay.json`` plus their sha256 ledger.

    Returns a mapping of artifact filename to the sha256 hex digest of the
    bytes that were written.  ``artifacts.sha256`` uses the standard
    ``sha256sum -c`` format, so the directory can be re-checked without this
    module.
    """
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    payloads = {
        EVIDENCE_FILENAME: json.dumps(evidence.to_dict(), indent=2).encode("utf-8"),
        REPLAY_FILENAME: json.dumps(replay.to_dict(), indent=2).encode("utf-8"),
    }
    digests: dict[str, str] = {}
    for filename, payload in payloads.items():
        (directory / filename).write_bytes(payload)
        digests[filename] = sha256(payload).hexdigest()
    ledger = "".join(f"{digest}  {filename}\n" for filename, digest in sorted(digests.items()))
    (directory / HASHES_FILENAME).write_text(ledger, encoding="utf-8")
    return digests


_EVIDENCE_REQUIRED_FIELDS = (
    "schema_version", "run_id", "match_id", "competitors", "phases", "events",
    "outcome", "tool_calls", "defenses", "attacks",
)
_REPLAY_REQUIRED_FIELDS = (
    "schema_version", "source_bundle_hash", "panes", "layout", "frames",
)
_FRAME_REQUIRED_FIELDS = (
    "sequence", "at_monotonic_ns", "event_id", "event_type", "phase", "pane", "text",
)


def validate_artifacts(out_dir: str | Path) -> list[str]:
    """Re-check hashes and required fields of a converted artifact directory.

    Returns a list of human-readable problems; an empty list means the
    directory is clean.  Detects missing files, unreadable JSON, missing or
    malformed required fields, and any artifact whose recorded sha256 no
    longer matches its bytes on disk (tamper detection).
    """
    directory = Path(out_dir)
    problems: list[str] = []

    parsed: dict[str, dict[str, Any]] = {}
    for filename, required_fields, expected_schema in (
        (EVIDENCE_FILENAME, _EVIDENCE_REQUIRED_FIELDS, EVIDENCE_SCHEMA),
        (REPLAY_FILENAME, _REPLAY_REQUIRED_FIELDS, REPLAY_SCHEMA),
    ):
        path = directory / filename
        if not path.is_file():
            problems.append(f"{filename}: missing")
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            problems.append(f"{filename}: unreadable ({error})")
            continue
        if not isinstance(document, dict):
            problems.append(f"{filename}: not an object")
            continue
        parsed[filename] = document
        problems.extend(
            f"{filename}: missing required field {field_name!r}"
            for field_name in required_fields
            if field_name not in document
        )
        if document.get("schema_version") != expected_schema:
            problems.append(
                f"{filename}: schema_version {document.get('schema_version')!r} "
                f"!= {expected_schema!r}"
            )

    evidence = parsed.get(EVIDENCE_FILENAME)
    if evidence is not None:
        competitors = evidence.get("competitors")
        if not isinstance(competitors, list) or any(not isinstance(item, str) for item in competitors):
            problems.append(f"{EVIDENCE_FILENAME}: competitors must be a list of strings")
        elif not competitors:
            problems.append(f"{EVIDENCE_FILENAME}: competitors must not be empty")
        phases = evidence.get("phases")
        if not isinstance(phases, list) or not phases:
            problems.append(f"{EVIDENCE_FILENAME}: phases must be a non-empty list")
        events = evidence.get("events")
        if not isinstance(events, list) or not events:
            problems.append(f"{EVIDENCE_FILENAME}: events must be a non-empty list")
        if not isinstance(evidence.get("outcome"), dict):
            problems.append(f"{EVIDENCE_FILENAME}: outcome must be an object")
        if not isinstance(evidence.get("tool_calls"), list):
            problems.append(f"{EVIDENCE_FILENAME}: tool_calls must be a list")
        if not isinstance(evidence.get("defenses"), dict):
            problems.append(f"{EVIDENCE_FILENAME}: defenses must be an object")
        if not isinstance(evidence.get("attacks"), list):
            problems.append(f"{EVIDENCE_FILENAME}: attacks must be a list")

    replay = parsed.get(REPLAY_FILENAME)
    frames: Any = None
    if replay is not None:
        bundle_hash = replay.get("source_bundle_hash")
        if not isinstance(bundle_hash, str) or len(bundle_hash) != 64:
            problems.append(f"{REPLAY_FILENAME}: source_bundle_hash must be 64 characters")
        if not isinstance(replay.get("panes"), list) or not replay.get("panes"):
            problems.append(f"{REPLAY_FILENAME}: panes must be a non-empty list")
        if not isinstance(replay.get("layout"), dict):
            problems.append(f"{REPLAY_FILENAME}: layout must be an object")
        frames = replay.get("frames")
        if not isinstance(frames, list) or not frames:
            problems.append(f"{REPLAY_FILENAME}: frames must be a non-empty list")
        else:
            previous_ns = -1
            for position, frame in enumerate(frames, start=1):
                if not isinstance(frame, dict):
                    problems.append(f"{REPLAY_FILENAME}: frame {position} is not an object")
                    continue
                missing = [key for key in _FRAME_REQUIRED_FIELDS if key not in frame]
                if missing:
                    problems.append(
                        f"{REPLAY_FILENAME}: frame {position} missing fields {','.join(missing)}"
                    )
                    continue
                if frame["sequence"] != position:
                    problems.append(
                        f"{REPLAY_FILENAME}: frame {position} has sequence "
                        f"{frame['sequence']!r}, expected {position}"
                    )
                timestamp = frame["at_monotonic_ns"]
                if not isinstance(timestamp, int):
                    problems.append(
                        f"{REPLAY_FILENAME}: frame {position} timestamp is not an integer"
                    )
                elif timestamp < previous_ns:
                    problems.append(f"{REPLAY_FILENAME}: frame {position} timestamp goes backwards")
                else:
                    previous_ns = timestamp
        if isinstance(frames, list) and isinstance(evidence, dict) and isinstance(evidence.get("events"), list):
            if len(frames) != len(evidence["events"]):
                problems.append(
                    f"frames/events disagree: {len(frames)} frames vs "
                    f"{len(evidence['events'])} events"
                )

    hashes_path = directory / HASHES_FILENAME
    if not hashes_path.is_file():
        problems.append(f"{HASHES_FILENAME}: missing")
        return problems
    try:
        lines = hashes_path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        problems.append(f"{HASHES_FILENAME}: unreadable ({error})")
        return problems
    recorded: dict[str, str] = {}
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        parts = line.split(None, 1)
        if len(parts) != 2 or len(parts[0]) != 64:
            problems.append(f"{HASHES_FILENAME}:{number}: malformed entry")
            continue
        recorded[parts[1].strip()] = parts[0]
    for filename in (EVIDENCE_FILENAME, REPLAY_FILENAME):
        path = directory / filename
        expected = recorded.pop(filename, None)
        if not path.is_file():
            if expected is not None:
                problems.append(f"{HASHES_FILENAME}: {filename} listed but missing on disk")
            continue
        if expected is None:
            problems.append(f"{HASHES_FILENAME}: no entry for {filename}")
            continue
        actual = sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            problems.append(f"{filename}: sha256 mismatch (recorded {expected}, actual {actual})")
    problems.extend(
        f"{HASHES_FILENAME}: unexpected entry for {extra}" for extra in sorted(recorded)
    )
    return problems
