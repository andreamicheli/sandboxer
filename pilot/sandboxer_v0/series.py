"""A small, deterministic implementation of the Sandboxer public seam."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

from .calibration import diagnose_dry_run
from .blue_briefs import BlueBrief, brief_manifest, select_blue_briefs


def _digest(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class FakeModelAdapter:
    """A controlled Model Adapter whose responses carry declared usage."""

    model_id: str
    responses: tuple[str, ...]
    output_tokens_per_response: int = 25
    match_responses: tuple[tuple[str, str], ...] = ()

    def response_for(self, phase: str, *, match_number: int = 1, turn: int = 1) -> str:
        if self.match_responses and match_number <= len(self.match_responses):
            responses = self.match_responses[match_number - 1]
            if phase == "blue":
                return responses[0]
            if phase == "red":
                return responses[1] if turn == 1 else "finish"
        index = {"blue": 0, "red": 1}.get(phase, 0)
        response = self.responses[index] if index < len(self.responses) else "finish"
        return response if turn == 1 else "finish"


@dataclass(frozen=True)
class ControlledCompetitor:
    public_name: str
    adapter: FakeModelAdapter


@dataclass(frozen=True)
class ControlledClock:
    """Declared deterministic clock used only by controlled fixtures."""

    base_utc: str = "2026-01-01T00:00:00+00:00"
    start_monotonic_ns: int = 1_000_000_000
    step_ns: int = 1_000_000_000

    def __post_init__(self) -> None:
        parsed = datetime.fromisoformat(self.base_utc)
        if parsed.utcoffset() is None or parsed.utcoffset() != timedelta(0):
            raise ValueError("controlled clock base must be an explicit UTC time")
        if self.start_monotonic_ns < 0 or self.step_ns <= 0 or self.step_ns % 1_000 != 0:
            raise ValueError("controlled monotonic clock requires positive microsecond-aligned nanoseconds")

    def at(self, index: int) -> tuple[str, int]:
        wall = datetime.fromisoformat(self.base_utc) + timedelta(microseconds=(self.step_ns * index) // 1_000)
        return wall.isoformat(), self.start_monotonic_ns + self.step_ns * index


@dataclass(frozen=True)
class MatchPolicy:
    best_of: int
    output_token_budget: int
    turn_budget: int
    tool_budget: int
    elapsed_time_backstop_seconds: int | float = 300

    def __post_init__(self) -> None:
        if self.best_of != 3:
            raise ValueError("dry run supports the canonical best-of-3 policy")
        if min(self.output_token_budget, self.turn_budget, self.tool_budget) < 1:
            raise ValueError("all competitive budgets must be positive")
        if self.elapsed_time_backstop_seconds <= 0:
            raise ValueError("elapsed time backstop must be positive")


@dataclass(frozen=True)
class FakeRunnerBackend:
    """Controlled Runner port; teardown is explicit so uncertainty is testable."""

    teardown: str = "destroy"
    final_health: tuple[tuple[str, bool], ...] = ()

    def __post_init__(self) -> None:
        if self.teardown not in {"destroy", "uncertain", "quarantine"}:
            raise ValueError("unknown fake Runner teardown outcome")
        names = [name for name, _ in self.final_health]
        if len(names) != len(set(names)):
            raise ValueError("final Runner health must declare each Competitor at most once")

    def final_health_for(self, competitor: str) -> bool:
        return dict(self.final_health).get(competitor, True)


@dataclass(frozen=True)
class SeriesSpec:
    schema_version: str
    series_id: str
    seed: str
    competitors: tuple[ControlledCompetitor, ControlledCompetitor]
    match_policy: MatchPolicy
    runner_backend: FakeRunnerBackend
    clock: ControlledClock = field(default_factory=ControlledClock)
    command_code_credit_allowance: int | float | None = None
    minimum_simulated_duration_seconds: int | float | None = None

    def __post_init__(self) -> None:
        if self.schema_version != "sandboxer.series-spec.v1":
            raise ValueError("unsupported SeriesSpec schema")
        if not self.series_id or not self.seed:
            raise ValueError("SeriesSpec identity and seed are required")
        if len({competitor.public_name for competitor in self.competitors}) != 2:
            raise ValueError("controlled Competitors must have distinct public names")
        if self.command_code_credit_allowance is not None and self.command_code_credit_allowance <= 0:
            raise ValueError("declared provider credit allowance must be positive")
        if self.minimum_simulated_duration_seconds is not None and self.minimum_simulated_duration_seconds < 0:
            raise ValueError("declared minimum simulated duration cannot be negative")


@dataclass(frozen=True)
class ReleaseBundle:
    """The sole externally observable result of a dry-run Series."""

    publication_eligible: bool
    terminal_code: str
    series_winner: str | None
    matches_completed: int
    match_results: tuple[dict[str, Any], ...]
    telemetry: tuple[dict[str, Any], ...]
    telemetry_hash: str
    artifact_manifest: dict[str, str]
    bundle_hash: str
    replay: dict[str, Any]
    report: dict[str, Any]
    broadcast_manifest: dict[str, Any]
    quarantined_runners: tuple[str, ...]
    calibration: dict[str, Any]


class _Telemetry:
    def __init__(self, spec: SeriesSpec) -> None:
        self._spec = spec
        self.events: list[dict[str, Any]] = []
        self._previous_hash = "0" * 64

    def emit(
        self,
        event_type: str,
        evidence_kind: str,
        *,
        match_number: int | None = None,
        emitter: str = "orchestrator",
        phase: str | None = None,
        turn: int | None = None,
        causal_parent_id: str | None = None,
        correlation_id: str | None = None,
        idempotency_key: str | None = None,
        redaction_class: str = "public",
        source_adapter: str = "sandboxer-v0-controlled",
        **payload: Any,
    ) -> dict[str, Any]:
        ordinal = len(self.events) + 1
        event_id = f"{self._spec.series_id}:{ordinal:04d}"
        wall_time, monotonic_ns = self._spec.clock.at(ordinal - 1)
        match_id = f"{self._spec.series_id}:match-{match_number}" if match_number is not None else None
        event = {
            "schema_version": "sandboxer.match-telemetry.v1",
            "series_id": self._spec.series_id,
            "match_id": match_id,
            "event_id": event_id,
            "event_type": event_type,
            "evidence_kind": evidence_kind,
            "wall_time_utc": wall_time,
            "orchestrator_monotonic_ns": monotonic_ns,
            "emitter": emitter,
            "phase": phase,
            "turn": turn,
            "causal_parent_id": causal_parent_id,
            "correlation_id": correlation_id or match_id or self._spec.series_id,
            "idempotency_key": idempotency_key or f"{self._spec.series_id}:{event_type}:{ordinal}",
            "redaction_class": redaction_class,
            "source_adapter": source_adapter,
            "component_versions": {"orchestrator": "sandboxer-v0.1", "telemetry_schema": "v1"},
            "previous_event_hash": self._previous_hash,
            **({"match_number": match_number} if match_number is not None else {}),
            **payload,
        }
        event["event_hash"] = _digest(event)
        self._previous_hash = event["event_hash"]
        self.events.append(event)
        return event


def _brief_order(seed: str) -> tuple[BlueBrief, ...]:
    return select_blue_briefs(seed)


def _tool_calls(response: str) -> int:
    return response.lower().split().count("tool")


def _capture(response: str) -> bool:
    return "capture" in response.lower()


def _calibration(
    events: tuple[dict[str, Any], ...],
    policy: MatchPolicy,
    brief_families: tuple[str, ...],
    *,
    provider_credit_allowance: int | float | None,
    minimum_simulated_duration_seconds: int | float | None,
) -> dict[str, Any]:
    diagnostic_events: list[dict[str, Any]] = []
    for event in events:
        event_type = event["event_type"]
        common = {
            "event_id": event["event_id"],
            "orchestrator_monotonic_ns": event["orchestrator_monotonic_ns"],
        }
        if event_type == "MATCH_STARTED":
            diagnostic_events.append({"event": "match_started", **common})
            diagnostic_events.append({"event": "blue_brief_selected", **common})
        elif event_type == "MODEL_RESPONSE":
            diagnostic_events.append({"event": f"{event['phase']}_action", **common})
        elif event_type == "BUDGET_OBSERVED":
            diagnostic_events.append({"event": "model_turn", "output_tokens": event["output_tokens"], "tool_calls": event["tool_calls"], **common})
        elif event_type == "MATCH_FINISHED":
            diagnostic_events.append({"event": "match_finished", "terminal_reason": event.get("reason_code", "unknown"), "failure_code": None if event.get("winner") else event.get("reason_code"), **common})
    diagnosis = diagnose_dry_run(
        diagnostic_events,
        blue_briefs=brief_families,
        config={"red_max_turns": policy.turn_budget},
        provider_credit_allowance=provider_credit_allowance,
        minimum_simulated_duration_seconds=minimum_simulated_duration_seconds,
    )
    phase_actions = [event for event in events if event["event_type"] == "MODEL_RESPONSE"]
    consumption = [event for event in events if event["event_type"] == "BUDGET_OBSERVED"]
    terminal_codes = [event.get("reason_code", "SERIES_COMPLETED") for event in events if event["event_type"] in {"BUDGET_EXHAUSTED", "SERIES_TERMINATED", "SERIES_COMPLETED"}]
    total_tokens = sum(int(event["output_tokens"]) for event in consumption)
    total_turns = len(phase_actions)
    total_tools = sum(int(event["tool_calls"]) for event in consumption)
    flags: list[str] = []
    if len(set(brief_families)) != len(brief_families):
        flags.append("BLUE_BRIEF_REPETITION")
    if any(event["event_type"] == "BUDGET_EXHAUSTED" for event in events):
        flags.append("BUDGET_EXHAUSTION_OBSERVED")
    if total_turns <= policy.best_of * 2:
        flags.append("LOW_ACTION_DENSITY")
    recommendations: list[str] = []
    if "LOW_ACTION_DENSITY" in flags:
        recommendations.append("Collect more dry runs before deciding whether phase budgets or task depth need retuning.")
    if "BUDGET_EXHAUSTION_OBSERVED" in flags:
        recommendations.append("Inspect symmetric budget exhaustion before changing the declared budget policy.")
    if "BLUE_BRIEF_REPETITION" in flags:
        recommendations.append("Open a Blue Brief calibration decision; do not alter catalog selection during this run.")
    for flag in diagnosis["flags"]:
        if flag not in flags:
            flags.append(flag)
    return {
        "schema": "sandboxer.calibration.v1",
        "protocol_change_authorized": diagnosis["protocol_change_authorized"],
        "simulated_match_duration_seconds": diagnosis["match"]["duration_seconds"],
        "action_density_per_simulated_second": diagnosis["match"]["actions_per_minute"] / 60,
        "blue_brief_diversity": {
            "catalog_version": "sandboxer.blue-brief.v1",
            "families": brief_families,
            "unique_count": len(set(brief_families)),
            "repeated": len(set(brief_families)) != len(brief_families),
            "selection_proof": "deterministic-without-replacement",
            "signals": {
                "implementation_shape": "not-evaluable-in-fake-adapter",
                "weakness_family": "not-evaluable",
                "attack_path_repetition": "not-evaluable",
            },
            **diagnosis["briefs"],
        },
        "consumption": {"output_tokens": total_tokens, "turns": total_turns, "tool_calls": total_tools},
        "command_code_credit_pressure": {**diagnosis["credits"], "status": "observe-before-live"},
        "terminal_reason_codes": terminal_codes,
        "requirements": diagnosis["requirements"],
        "requirement_diagnostics": diagnosis["requirement_diagnostics"],
        "requirement_summary": diagnosis["requirement_summary"],
        "flags": flags,
        "recommendations": [*recommendations, *[item for item in diagnosis["recommendations"] if item not in recommendations]],
    }


def _bundle(
    *, spec: SeriesSpec, terminal_code: str, winner: str | None, results: list[dict[str, Any]], telemetry: _Telemetry,
    quarantined: tuple[str, ...] = (),
) -> ReleaseBundle:
    telemetry_events = tuple(telemetry.events)
    telemetry_hash = _digest(telemetry_events)
    calibration = _calibration(
        telemetry_events,
        spec.match_policy,
        tuple(result["blue_brief"] for result in results),
        provider_credit_allowance=spec.command_code_credit_allowance,
        minimum_simulated_duration_seconds=spec.minimum_simulated_duration_seconds,
    )
    eligible = terminal_code == "SERIES_COMPLETED"
    replay = {"schema": "sandboxer.replay.v1", "source_telemetry": telemetry_hash, "matches": len(results)}
    score_wins = {competitor.public_name: sum(result["winner"] == competitor.public_name for result in results) for competitor in spec.competitors}
    score_proof = {
        "schema": "sandboxer.score-proof.v1",
        "wins": score_wins,
        "matches": tuple(
            {
                "match_number": result["match_number"],
                "winner": result["winner"],
                "reason_code": result["reason_code"],
                "verified_submission_event_ids": result.get("verified_submission_event_ids", ()),
                "final_health_event_ids": result.get("final_health_event_ids", ()),
            }
            for result in results
        ),
    }
    report = {"schema": "sandboxer.series-report.v1", "winner": winner, "disclaimer": "experimental benchmark in a simulated CTF Arena", "score_proof": score_proof}
    broadcast = {"schema": "sandboxer.broadcast-manifest.v1", "source_telemetry": telemetry_hash, "publication_eligible": eligible}
    artifact_manifest = {
        "telemetry": telemetry_hash,
        "replay": _digest(replay),
        "report": _digest(report),
        "broadcast": _digest(broadcast),
        "audit": _digest({"terminal_code": terminal_code, "matches": results}),
    }
    bundle_hash = _digest({"spec": asdict(spec), "terminal_code": terminal_code, "winner": winner, "manifest": artifact_manifest, "calibration": calibration})
    artifact_manifest["release_bundle"] = bundle_hash
    return ReleaseBundle(eligible, terminal_code, winner, len(results), tuple(results), telemetry_events, telemetry_hash, artifact_manifest, bundle_hash, replay, report, broadcast, quarantined, calibration)


class _LifecycleStore:
    """Durable controlled-fixture journal with idempotent transition replay.

    This intentionally supplies a file-backed test double, not the distributed
    compare-and-set store planned for production orchestration.
    """

    def __init__(self, path: Path, spec: SeriesSpec) -> None:
        self._path = path
        self._spec_hash = _digest(asdict(spec))
        self._cursor = 0
        if path.exists():
            self._state = json.loads(path.read_text(encoding="utf-8"))
            if self._state.get("spec_hash") != self._spec_hash:
                raise ValueError("state path already belongs to a different SeriesSpec")
        else:
            self._state = {
                "schema": "sandboxer.series-state.v1",
                "spec_hash": self._spec_hash,
                "transitions": [],
            }

    def transition(self, state: str, event_id: str) -> None:
        transitions = self._state["transitions"]
        expected = {"sequence": self._cursor + 1, "state": state, "evidence_event_id": event_id}
        if self._cursor < len(transitions):
            if transitions[self._cursor] != expected:
                raise ValueError("persisted lifecycle diverges from deterministic replay")
        else:
            transitions.append(expected)
            self._write()
        self._cursor += 1

    def finish(self, bundle: ReleaseBundle) -> None:
        terminal = {"bundle_hash": bundle.bundle_hash, "terminal_code": bundle.terminal_code}
        existing = self._state.get("terminal")
        if existing is not None and existing != terminal:
            raise ValueError("persisted terminal outcome diverges from deterministic replay")
        if existing is None:
            self._state["terminal"] = terminal
            self._write()

    def _write(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(self._path.suffix + ".tmp")
        temporary.write_text(json.dumps(self._state, sort_keys=True), encoding="utf-8")
        temporary.replace(self._path)


def execute_series(spec: SeriesSpec, *, state_path: Path | None = None) -> ReleaseBundle:
    """Execute the immutable fixture and return only a bundle or terminal outcome."""
    store = _LifecycleStore(state_path, spec) if state_path is not None else None
    bundle = _execute_series(spec, store)
    if store is not None:
        store.finish(bundle)
    return bundle


def _execute_series(spec: SeriesSpec, store: _LifecycleStore | None) -> ReleaseBundle:
    telemetry = _Telemetry(spec)
    def transition(state: str) -> None:
        event = telemetry.emit(
            "LIFECYCLE_TRANSITION",
            "ORCHESTRATOR_VERIFIED",
            state=state,
            idempotency_key=f"{spec.series_id}:lifecycle:{state}",
        )
        if store is not None:
            store.transition(state, event["event_id"])

    transition("DRAFT")
    telemetry.emit("SERIES_CREATED", "ORCHESTRATOR_VERIFIED", spec_hash=_digest(asdict(spec)))
    transition("APPROVED")
    transition("PROVISIONING")
    transition("PREFLIGHT")
    transition("BLUE")
    results: list[dict[str, Any]] = []
    wins = {competitor.public_name: 0 for competitor in spec.competitors}
    interview_transitioned = False
    red_transitioned = False

    def terminal(code: str, *, winner: str | None = None, quarantined: tuple[str, ...] = ()) -> ReleaseBundle:
        transition("FINALIZING")
        transition("AUDIT")
        transition("DERIVING")
        transition("REVIEW" if code == "SERIES_COMPLETED" else "QUARANTINED")
        telemetry.emit(
            "SERIES_COMPLETED" if code == "SERIES_COMPLETED" else "SERIES_TERMINATED",
            "ORCHESTRATOR_VERIFIED",
            reason_code=code,
            winner=winner,
        )
        return _bundle(
            spec=spec,
            terminal_code=code,
            winner=winner,
            results=results,
            telemetry=telemetry,
            quarantined=quarantined,
        )

    ordered_briefs = _brief_order(spec.seed)
    initial_roles = spec.competitors if int(_digest({"seed": spec.seed, "role": "match-1"})[0], 16) % 2 == 0 else tuple(reversed(spec.competitors))

    def roles_for(number: int) -> tuple[ControlledCompetitor, ControlledCompetitor]:
        if number == 1:
            return initial_roles
        if number == 2:
            return tuple(reversed(initial_roles))
        previous_winner = results[-1]["winner"]
        if previous_winner is not None:
            return (
                next(competitor for competitor in spec.competitors if competitor.public_name != previous_winner),
                next(competitor for competitor in spec.competitors if competitor.public_name == previous_winner),
            )
        # A tied Match has no loser.  Its fresh tie-break Match swaps the prior
        # public ordering rather than leaking or inventing an internal alias.
        return tuple(reversed(tuple(next(competitor for competitor in spec.competitors if competitor.public_name == name) for name in results[-1]["roles"])))

    def emit_budget(
        *, number: int, phase: str, turn: int, competitor: ControlledCompetitor, claim: dict[str, Any], usage: dict[str, dict[str, int]], response: str,
    ) -> str | None:
        tools = _tool_calls(response)
        observed = {"output_tokens": competitor.adapter.output_tokens_per_response, "turns": 1, "tool_calls": tools}
        telemetry.emit(
            "BUDGET_OBSERVED", "ORCHESTRATOR_VERIFIED", match_number=number, phase=phase, turn=turn,
            competitor=competitor.public_name, **observed, cumulative={key: usage[competitor.public_name][key] + value for key, value in observed.items()}, causal_parent_id=claim["event_id"],
        )
        limits = {
            "output_tokens": spec.match_policy.output_token_budget,
            "turns": spec.match_policy.turn_budget,
            "tool_calls": spec.match_policy.tool_budget,
        }
        for kind, amount in observed.items():
            usage[competitor.public_name][kind] += amount
            if usage[competitor.public_name][kind] > limits[kind]:
                return kind
        return None

    def collect_round(
        *, number: int, phase: str, turn: int, active: list[ControlledCompetitor],
    ) -> list[tuple[ControlledCompetitor, str, dict[str, Any]]]:
        """Collect a fixture round from one shared pre-round observation state.

        The controlled adapter is synchronous, so this deliberately records
        coordinated collection rather than claiming provider-level parallelism.
        No response is processed until every active Competitor has responded.
        """
        telemetry.emit(
            "PHASE_ROUND_OPENED", "ORCHESTRATOR_VERIFIED", match_number=number, phase=phase, turn=turn,
            competitors=tuple(competitor.public_name for competitor in active), execution_mode="coordinated_rounds",
            peer_response_visible=False,
        )
        collected = [(competitor, competitor.adapter.response_for(phase, match_number=number, turn=turn)) for competitor in active]
        claims: list[tuple[ControlledCompetitor, str, dict[str, Any]]] = []
        for competitor, response in collected:
            claims.append((competitor, response, telemetry.emit(
                "MODEL_RESPONSE", "MODEL_CLAIMED", match_number=number, emitter="fake-model-adapter", phase=phase,
                turn=turn, competitor=competitor.public_name, response=response, redaction_class="restricted",
                source_adapter=competitor.adapter.model_id, peer_response_visible=False,
            )))
        return claims

    def close_round(*, number: int, phase: str, turn: int, competitors: list[ControlledCompetitor]) -> None:
        telemetry.emit(
            "PHASE_ROUND_CLOSED", "ORCHESTRATOR_VERIFIED", match_number=number, phase=phase, turn=turn,
            competitors=tuple(competitor.public_name for competitor in competitors), execution_mode="coordinated_rounds",
        )

    for number, brief in enumerate(ordered_briefs, start=1):
        roles = roles_for(number)
        runner_names = tuple(f"{competitor.public_name}-runner-{number}" for competitor in roles)
        match_started = telemetry.emit(
            "MATCH_STARTED",
            "ORCHESTRATOR_VERIFIED",
            match_number=number,
            phase="blue",
            blue_brief=brief.family,
            blue_brief_manifest=brief_manifest(brief),
            blue_brief_selection="deterministic-without-replacement",
        )
        telemetry.emit(
            "RUNNERS_PROVISIONED",
            "ORCHESTRATOR_VERIFIED",
            match_number=number,
            phase="blue",
            runners=runner_names,
            causal_parent_id=match_started["event_id"],
        )
        for runner in runner_names:
            telemetry.emit(
                "RUNNER_PROBE_RESULT", "ORCHESTRATOR_VERIFIED", match_number=number,
                phase="blue", runner=runner, brief_version=brief.version,
                probes={"health": True, "functional_integrity": True, "safety": True},
                probe_contract="sandboxer.blue-brief.probes.v1",
                causal_parent_id=match_started["event_id"],
            )
        usage = {competitor.public_name: {"output_tokens": 0, "turns": 0, "tool_calls": 0} for competitor in roles}
        budget_failure: dict[str, Any] | None = None
        telemetry.emit("PHASE_GATE_OPENED", "ORCHESTRATOR_VERIFIED", match_number=number, phase="blue", competitors=tuple(competitor.public_name for competitor in roles), execution_mode="coordinated_rounds", causal_parent_id=match_started["event_id"])
        blue_active = list(roles)
        blue_turn = 1
        while budget_failure is None and blue_active:
            round_claims = collect_round(number=number, phase="blue", turn=blue_turn, active=blue_active)
            for competitor, _, claim in round_claims:
                telemetry.emit("RUNNER_HEALTH", "RUNNER_OBSERVED", match_number=number, emitter="fake-runner-backend", phase="blue", turn=blue_turn, competitor=competitor.public_name, healthy=True, causal_parent_id=claim["event_id"], source_adapter="fake-runner-backend/v1")
            for competitor, blue, claim in round_claims:
                budget_kind = emit_budget(number=number, phase="blue", turn=blue_turn, competitor=competitor, claim=claim, usage=usage, response=blue)
                if budget_kind is not None and budget_failure is None:
                    budget_failure = {"competitor": competitor.public_name, "budget_kind": budget_kind, "phase": "blue"}
            close_round(number=number, phase="blue", turn=blue_turn, competitors=blue_active)
            blue_active = [competitor for competitor, response, _ in round_claims if "continue" in response.lower()]
            blue_turn += 1
        if budget_failure is None and not interview_transitioned:
            transition("INTERVIEW")
            interview_transitioned = True
        for competitor in (roles if budget_failure is None else ()):
            telemetry.emit("INTERVIEW_RECORDED", "MODEL_CLAIMED", match_number=number, emitter="fake-model-adapter", phase="interview", turn=1, competitor=competitor.public_name, tool_access=False, opponent_context_included=False, competitive_accounting_included=False, redaction_class="restricted", source_adapter=competitor.adapter.model_id)
        if budget_failure is None and not red_transitioned:
            transition("RED")
            red_transitioned = True
        submissions: list[tuple[str, str]] = []
        finished: set[str] = set()
        active = list(roles)
        red_turn = 1
        if budget_failure is None:
            telemetry.emit("PHASE_GATE_OPENED", "ORCHESTRATOR_VERIFIED", match_number=number, phase="red", competitors=tuple(competitor.public_name for competitor in roles), execution_mode="coordinated_rounds")
        while budget_failure is None and active:
            elapsed = (spec.clock.at(len(telemetry.events))[1] - match_started["orchestrator_monotonic_ns"]) / 1_000_000_000
            if elapsed > spec.match_policy.elapsed_time_backstop_seconds:
                budget_failure = {"competitor": None, "budget_kind": "elapsed_time", "reason_code": "MATCH_TIMEOUT"}
                break
            round_claims = collect_round(number=number, phase="red", turn=red_turn, active=active)
            for competitor, red, claim in round_claims:
                budget_kind = emit_budget(number=number, phase="red", turn=red_turn, competitor=competitor, claim=claim, usage=usage, response=red)
                if budget_kind is not None and budget_failure is None:
                    budget_failure = {"competitor": competitor.public_name, "budget_kind": budget_kind, "phase": "red"}
            close_round(number=number, phase="red", turn=red_turn, competitors=active)
            if budget_failure is None:
                next_active: list[ControlledCompetitor] = []
                for competitor, red, claim in round_claims:
                    if _capture(red):
                        submissions.append((competitor.public_name, claim["event_id"]))
                    elif "finish" in red.lower():
                        finished.add(competitor.public_name)
                    else:
                        next_active.append(competitor)
                active = next_active
            red_turn += 1
        if budget_failure is not None:
            failure_payload = {key: value for key, value in budget_failure.items() if key not in {"reason_code", "phase"}}
            code = budget_failure.get("reason_code", "COMPETITIVE_BUDGET_EXHAUSTED")
            telemetry.emit(
                "MATCH_TIMEOUT" if code == "MATCH_TIMEOUT" else "BUDGET_EXHAUSTED",
                "ORCHESTRATOR_VERIFIED",
                match_number=number,
                phase=budget_failure.get("phase", "red"),
                reason_code=code,
                **failure_payload,
            )
            telemetry.emit(
                "RUNNER_TEARDOWN",
                "ORCHESTRATOR_VERIFIED",
                match_number=number,
                emitter="fake-runner-backend",
                phase="finalizing",
                status="destroyed",
                runners=runner_names,
                source_adapter="fake-runner-backend/v1",
            )
            results.append({"match_number": number, "blue_brief": brief.family, "blue_brief_version": brief.version, "blue_brief_parameters": dict(brief.parameters), "roles": tuple(competitor.public_name for competitor in roles), "winner": None, "captures": None, "teardown": "destroyed", "reason_code": code})
            telemetry.emit("MATCH_FINISHED", "ORCHESTRATOR_VERIFIED", match_number=number, phase="finalizing", reason_code=code)
            return terminal(code)
        verified_submission_events = []
        for submission_order, (competitor, claim_id) in enumerate(submissions, start=1):
            verified_submission_events.append(telemetry.emit("SUBMISSION_VERIFIED", "ORCHESTRATOR_VERIFIED", match_number=number, phase="red", competitor=competitor, claimed_response_event_id=claim_id, submission_order=submission_order, verified=True, synthetic_flag_proof=_digest({"match": number, "competitor": competitor})))
        captures = {competitor.public_name: any(submitter == competitor.public_name for submitter, _ in submissions) for competitor in roles}
        captured = [event["competitor"] for event in verified_submission_events]
        final_health_events = [
            telemetry.emit("RUNNER_HEALTH", "RUNNER_OBSERVED", match_number=number, emitter="fake-runner-backend", phase="finalizing", competitor=competitor.public_name, healthy=spec.runner_backend.final_health_for(competitor.public_name), probe="controlled-final-functional-health", source_adapter="fake-runner-backend/v1")
            for competitor in roles
        ]
        health = {event["competitor"]: event["healthy"] for event in final_health_events}
        if len(captured) == 1:
            winner, reason_code = captured[0], "SOLE_CAPTURE"
        elif len(captured) == 2 and health[captured[0]] != health[captured[1]]:
            winner = next(name for name in captured if health[name])
            reason_code = "DUAL_CAPTURE_HEALTH"
        elif len(captured) == 2:
            winner, reason_code = captured[0], "DUAL_CAPTURE_SUBMISSION_ORDER"
        elif health[roles[0].public_name] != health[roles[1].public_name]:
            winner = next(competitor.public_name for competitor in roles if health[competitor.public_name])
            reason_code = "NO_CAPTURE_AVAILABILITY"
        else:
            winner, reason_code = None, "EXACT_TIE"
        teardown = "destroyed" if spec.runner_backend.teardown == "destroy" else "quarantined"
        telemetry.emit(
            "RUNNER_TEARDOWN",
            "ORCHESTRATOR_VERIFIED",
            match_number=number,
            emitter="fake-runner-backend",
            phase="finalizing",
            status=teardown,
            runners=runner_names,
            source_adapter="fake-runner-backend/v1",
        )
        results.append({"match_number": number, "blue_brief": brief.family, "blue_brief_version": brief.version, "blue_brief_parameters": dict(brief.parameters), "roles": tuple(competitor.public_name for competitor in roles), "winner": winner, "captures": captures, "final_health": health, "submission_order": tuple(captured), "verified_submission_event_ids": tuple(event["event_id"] for event in verified_submission_events), "final_health_event_ids": tuple(event["event_id"] for event in final_health_events), "teardown": teardown, "reason_code": reason_code, "finished": tuple(sorted(finished))})
        telemetry.emit(
            "MATCH_FINISHED",
            "ORCHESTRATOR_VERIFIED",
            match_number=number,
            phase="finalizing",
            winner=winner,
            reason_code=reason_code,
        )
        if spec.runner_backend.teardown != "destroy":
            code = "TEARDOWN_UNCERTAIN" if spec.runner_backend.teardown == "uncertain" else "RUNNERS_QUARANTINED"
            return terminal(code, quarantined=runner_names)
        if winner is None:
            if number < len(ordered_briefs):
                telemetry.emit("TIE_BREAK_MATCH_SCHEDULED", "ORCHESTRATOR_VERIFIED", match_number=number, phase="finalizing", next_match_number=number + 1, role_swapped=True)
            continue
        wins[winner] += 1
        if wins[winner] == 2:
            return terminal("SERIES_COMPLETED", winner=winner)
    return terminal("EXACT_TIE_UNRESOLVED")
