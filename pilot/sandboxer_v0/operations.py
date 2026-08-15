"""Durable, approval-gated orchestration for supervised and batch-lab Series.

This module is the control-plane seam.  It deliberately consumes the existing
credential-free ``SeriesSpec -> ReleaseBundle`` seam and does not provision or
otherwise reach a Runner implementation directly.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from enum import StrEnum
import json
import math
from pathlib import Path
import threading
import time
from typing import Any, Callable, Iterator, Protocol
import fcntl
from contextlib import contextmanager

from .series import (
    ControlledClock,
    ControlledCompetitor,
    FakeModelAdapter,
    FakeRunnerBackend,
    MatchPolicy,
    ReleaseBundle,
    SeriesSpec,
    execute_series,
)


class OperationMode(StrEnum):
    SUPERVISED = "supervised"
    BATCH_LAB = "batch-lab"


class CapacityCondition(StrEnum):
    AVAILABLE = "AVAILABLE"
    RECOVERABLE_CAPACITY = "RECOVERABLE_CAPACITY"
    RECOVERABLE_CREDITS = "RECOVERABLE_CREDITS"
    AUTH_FAILURE = "AUTH_FAILURE"
    CATALOG_DRIFT = "CATALOG_DRIFT"
    POLICY_INCOMPATIBLE = "POLICY_INCOMPATIBLE"
    SAFETY_FAILURE = "SAFETY_FAILURE"


@dataclass(frozen=True)
class CapacityObservation:
    """Provider-facing observation; only capacity and credit conditions may wait."""

    condition: CapacityCondition
    wait_cost: int | float = 0
    execution_cost: int | float = 0

    def __post_init__(self) -> None:
        if self.wait_cost < 0 or self.execution_cost < 0:
            raise ValueError("provider costs cannot be negative")


class ProviderAvailability(Protocol):
    def observe(self, spec: SeriesSpec) -> CapacityObservation: ...


class SeriesExecutor(Protocol):
    def __call__(self, spec: SeriesSpec) -> ReleaseBundle: ...

    # Optional: a production Adapter must implement bounded cancellation and
    # return teardown observations.  The controlled default has no live work.


@dataclass(frozen=True)
class ExecutionResult:
    """Executor completion with measured spend, not an admission estimate."""

    bundle: ReleaseBundle
    actual_cost: int | float = 0

    def __post_init__(self) -> None:
        if not isinstance(self.actual_cost, (int, float)) or isinstance(self.actual_cost, bool) or not math.isfinite(self.actual_cost) or self.actual_cost < 0:
            raise ValueError("actual execution cost must be finite and non-negative")


@dataclass(frozen=True)
class OperationControls:
    """Declared waiting, spend, and TTL limits for one submitted Series."""

    wait_deadline_seconds: int | float = 3600
    initial_backoff_seconds: int | float = 5
    max_backoff_seconds: int | float = 300
    wait_cost_ceiling: int | float = 0
    execution_cost_ceiling: int | float = 0
    runner_ttl_seconds: int | float = 900
    cleanup_timeout_seconds: int | float = 30

    def __post_init__(self) -> None:
        if self.wait_deadline_seconds <= 0 or self.initial_backoff_seconds <= 0:
            raise ValueError("wait deadline and initial backoff must be positive")
        if self.max_backoff_seconds < self.initial_backoff_seconds:
            raise ValueError("maximum backoff cannot be below initial backoff")
        if min(self.wait_cost_ceiling, self.execution_cost_ceiling) < 0 or self.runner_ttl_seconds <= 0 or self.cleanup_timeout_seconds <= 0:
            raise ValueError("cost ceilings must be non-negative and Runner TTL positive")


class StaleRevision(ValueError):
    """A persisted compare-and-set revision did not match the caller's view."""


class InvalidTransition(ValueError):
    """A requested operation is not legal from the persisted stage."""


class PublicationBlocked(InvalidTransition):
    """Batch-lab artifacts can never advance a public publication pointer."""


@dataclass(frozen=True)
class OperationSnapshot:
    series_id: str
    mode: OperationMode
    series_state: str
    series_revision: int
    publication_state: str
    publication_revision: int
    publication_pointer: str | None
    artifact_label: str | None
    reason_code: str | None
    wait_attempts: int
    next_attempt_at: int | float | None
    tracked_runners: tuple[str, ...]
    matches: tuple[dict[str, object], ...]
    match_policy: dict[str, int | float]
    artifact_reference: str | None = None
    evidence_version: int | None = None
    spend: dict[str, int | float] | None = None
    queue_position: int | None = None


class _AlwaysAvailable:
    def observe(self, _spec: SeriesSpec) -> CapacityObservation:
        return CapacityObservation(CapacityCondition.AVAILABLE)


_RECOVERABLE = {CapacityCondition.RECOVERABLE_CAPACITY, CapacityCondition.RECOVERABLE_CREDITS}
_TERMINAL = {"COMPLETED", "BLOCKED", "QUARANTINED", "CANCELLED"}


class SeriesOperations:
    """A file-backed compare-and-set journal and serial Series queue.

    The public interface presents one submission and its snapshot.  Internally,
    Series, individual Match declarations, Publication, and Runner inventory
    are stored in separate records so recovery can quarantine incomplete work
    without inventing a terminal result.
    """

    def __init__(
        self,
        path: Path,
        *,
        availability: ProviderAvailability | None = None,
        executor: SeriesExecutor = execute_series,
        clock: Callable[[], int | float] | None = None,
    ) -> None:
        self._path = path
        self._availability = availability or _AlwaysAvailable()
        self._executor = executor
        self._clock = clock or time.time
        self._state: dict[str, Any] = {}
        self._reload()

    @contextmanager
    def _lock(self) -> Iterator[None]:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self._path.with_suffix(self._path.suffix + ".lock")
        with lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            self._reload()
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _reload(self) -> None:
        if self._path.exists():
            self._state = json.loads(self._path.read_text(encoding="utf-8"))
            if self._state.get("schema") != "sandboxer.operations.v1":
                raise ValueError("unsupported operations state schema")
        else:
            self._state = {"schema": "sandboxer.operations.v1", "queue": [], "series": {}, "matches": {}, "publication": {}, "runners": {}, "artifacts": {}}

    def submit(
        self,
        spec: SeriesSpec,
        *,
        mode: OperationMode,
        controls: OperationControls = OperationControls(),
        submitted_at: int | float | None = None,
    ) -> OperationSnapshot:
        with self._lock():
            return self._submit(spec, mode=mode, controls=controls, submitted_at=self._clock() if submitted_at is None else submitted_at)

    def _submit(self, spec: SeriesSpec, *, mode: OperationMode, controls: OperationControls, submitted_at: int | float) -> OperationSnapshot:
        if spec.series_id in self._state["series"]:
            raise InvalidTransition("SERIES_ID_ALREADY_EXISTS")
        series_state = "AWAITING_SPEND_APPROVAL" if mode is OperationMode.SUPERVISED else "QUEUED"
        self._state["series"][spec.series_id] = {
            "revision": 1,
            "state": series_state,
            "mode": mode.value,
            "spec": asdict(spec),
            "controls": asdict(controls),
            "reason_code": None,
            "wait_attempts": 0,
            "next_attempt_at": None,
            "submitted_at": submitted_at,
            "wait_cost_spent": 0,
            "execution_cost_spent": 0,
        }
        self._state["publication"][spec.series_id] = {
            "revision": 1,
            "state": "DRAFT",
            "pointer": None,
            "artifact_label": None,
        }
        for number in range(1, spec.match_policy.best_of + 1):
            match_id = f"{spec.series_id}:match-{number}"
            self._state["matches"][match_id] = {"revision": 1, "series_id": spec.series_id, "state": "DECLARED", "reason_code": None}
        self._state["queue"].append(spec.series_id)
        self._write()
        return self._snapshot(spec.series_id)

    def snapshot(self, series_id: str) -> OperationSnapshot:
        with self._lock():
            return self._snapshot(series_id)

    def _snapshot(self, series_id: str) -> OperationSnapshot:
        series = self._series(series_id)
        publication = self._state["publication"][series_id]
        spec = _decode_spec(series["spec"])
        matches = tuple(
            {"match_id": match_id, "state": record["state"], "reason_code": record["reason_code"]}
            for match_id, record in self._state["matches"].items()
            if record["series_id"] == series_id
        )
        runners = tuple(resource_id for resource_id, record in self._state["runners"].items() if record["series_id"] == series_id)
        active_queue = [item for item in self._state["queue"] if self._state["series"][item]["state"] not in _TERMINAL]
        queue_position = active_queue.index(series_id) + 1 if series_id in active_queue else None
        return OperationSnapshot(
            series_id=series_id,
            mode=OperationMode(series["mode"]),
            series_state=series["state"],
            series_revision=series["revision"],
            publication_state=publication["state"],
            publication_revision=publication["revision"],
            publication_pointer=publication["pointer"],
            artifact_label=publication["artifact_label"],
            reason_code=series["reason_code"],
            wait_attempts=series["wait_attempts"],
            next_attempt_at=series["next_attempt_at"],
            tracked_runners=runners,
            matches=matches,
            match_policy={
                "best_of": spec.match_policy.best_of,
                "output_token_budget": spec.match_policy.output_token_budget,
                "turn_budget": spec.match_policy.turn_budget,
                "tool_budget": spec.match_policy.tool_budget,
            },
            artifact_reference=self._state.get("artifacts", {}).get(series_id, {}).get("reference"),
            evidence_version=self._state.get("artifacts", {}).get(series_id, {}).get("evidence_version"),
            spend={"wait": series["wait_cost_spent"], "execution": series["execution_cost_spent"]},
            queue_position=queue_position,
        )

    def approve_spend(self, series_id: str, *, expected_revision: int) -> OperationSnapshot:
        with self._lock():
            return self._approve_spend(series_id, expected_revision)

    def _approve_spend(self, series_id: str, expected_revision: int) -> OperationSnapshot:
        series = self._series(series_id)
        self._require_revision(series, expected_revision)
        if series["mode"] != OperationMode.SUPERVISED.value or series["state"] != "AWAITING_SPEND_APPROVAL":
            raise InvalidTransition("SPEND_APPROVAL_NOT_PENDING")
        self._update(series, state="QUEUED")
        self._write()
        return self._snapshot(series_id)

    def approve_publication(self, series_id: str, *, expected_revision: int) -> OperationSnapshot:
        with self._lock():
            return self._approve_publication(series_id, expected_revision)

    def _approve_publication(self, series_id: str, expected_revision: int) -> OperationSnapshot:
        series = self._series(series_id)
        publication = self._state["publication"][series_id]
        self._require_revision(publication, expected_revision)
        if series["mode"] == OperationMode.BATCH_LAB.value:
            raise PublicationBlocked("BATCH_LAB_PUBLICATION_FORBIDDEN")
        if publication["state"] != "AWAITING_PUBLICATION_APPROVAL":
            raise InvalidTransition("PUBLICATION_APPROVAL_NOT_PENDING")
        artifact = self._state["artifacts"].get(series_id)
        if not artifact or not artifact.get("reference"):
            raise InvalidTransition("FROZEN_EVIDENCE_REQUIRED")
        self._update(publication, state="PUBLISHED", pointer=artifact["reference"])
        self._write()
        return self._snapshot(series_id)

    def advance(self, *, now: int | float) -> tuple[OperationSnapshot, ...]:
        """Advance exactly the first runnable queue item, preserving serial order."""
        with self._lock():
            prepared = self._advance_prepare(now)
        if isinstance(prepared, tuple) and prepared and prepared[0] == "__EXECUTE__":
            _, series_id, spec, revision = prepared
            return self._execute_after_prepare(series_id, spec, revision)
        return prepared

    def _advance_prepare(self, now: int | float) -> tuple[OperationSnapshot, ...]:
        series_id = self._next_series(now)
        if series_id is None:
            return ()
        series = self._series(series_id)
        spec = _decode_spec(series["spec"])
        controls = OperationControls(**series["controls"])
        # Submission time is persisted at enqueue; queue wait consumes deadline.
        if now > series["submitted_at"] + controls.wait_deadline_seconds:
            self._block(series_id, "WAIT_DEADLINE_EXCEEDED")
            return (self._snapshot(series_id),)
        observation = self._availability.observe(spec)
        if observation.condition in _RECOVERABLE:
            if series["wait_cost_spent"] + observation.wait_cost > controls.wait_cost_ceiling:
                self._block(series_id, "WAIT_COST_CEILING_EXCEEDED")
            else:
                delay = min(controls.initial_backoff_seconds * (2 ** series["wait_attempts"]), controls.max_backoff_seconds)
                self._update(
                    series,
                    state="WAITING_FOR_CAPACITY",
                    wait_attempts=series["wait_attempts"] + 1,
                    wait_cost_spent=series["wait_cost_spent"] + observation.wait_cost,
                    next_attempt_at=now + delay,
                    reason_code=observation.condition.value,
                )
                self._write()
            return (self._snapshot(series_id),)
        if observation.condition is not CapacityCondition.AVAILABLE:
            self._block(series_id, observation.condition.value)
            return (self._snapshot(series_id),)
        if series["execution_cost_spent"] + observation.execution_cost > controls.execution_cost_ceiling:
            self._block(series_id, "EXECUTION_COST_CEILING_EXCEEDED")
            return (self._snapshot(series_id),)

        self._mark_running(series_id, observation.execution_cost, now)
        execution_revision = self._series(series_id)["revision"]
        # The sentinel makes execution occur only after the lock is released.
        return ("__EXECUTE__", series_id, spec, execution_revision)

    def _execute_after_prepare(self, series_id: str, spec: SeriesSpec, execution_revision: int) -> tuple[OperationSnapshot, ...]:
        try:
            completed = self._executor(spec)
        except Exception:
            with self._lock():
                if self._series(series_id)["state"] == "RUNNING":
                    self._quarantine(series_id, "EXECUTION_FAILED")
                return (self._snapshot(series_id),)
        with self._lock():
            series = self._series(series_id)
            if series["state"] != "RUNNING" or series["revision"] != execution_revision:
                return (self._snapshot(series_id),)
            result = completed if isinstance(completed, ExecutionResult) else ExecutionResult(completed)
            controls = OperationControls(**series["controls"])
            if series["execution_cost_spent"] - series.get("reserved_execution_cost", 0) + result.actual_cost > controls.execution_cost_ceiling:
                self._quarantine(series_id, "EXECUTION_COST_CEILING_EXCEEDED")
                return (self._snapshot(series_id),)
            self._update(series, execution_cost_spent=series["execution_cost_spent"] - series.get("reserved_execution_cost", 0) + result.actual_cost, reserved_execution_cost=0)
            self._record_bundle(series_id, result.bundle)
            return (self._snapshot(series_id),)

    def cancel(self, series_id: str, *, expected_revision: int) -> OperationSnapshot:
        with self._lock():
            series = self._series(series_id)
            self._require_revision(series, expected_revision)
            was_running = series["state"] == "RUNNING"
            snapshot = self._cancel(series_id, expected_revision)
        if was_running:
            self._apply_cleanup(series_id, "OPERATOR_CANCELLED")
            return self.snapshot(series_id)
        return snapshot

    def _cancel(self, series_id: str, expected_revision: int) -> OperationSnapshot:
        series = self._series(series_id)
        self._require_revision(series, expected_revision)
        if series["state"] in _TERMINAL:
            raise InvalidTransition("SERIES_ALREADY_TERMINAL")
        was_running = series["state"] == "RUNNING"
        self._update(series, state="CANCELLED", reason_code="OPERATOR_CANCELLED", next_attempt_at=None)
        self._update(self._state["publication"][series_id], state="CANCELLED")
        self._set_matches(series_id, "CANCELLED", only_declared=True)
        if not was_running:
            self._quarantine_runners(series_id, "OPERATOR_CANCELLED")
        self._write()
        return self._snapshot(series_id)

    def recover(self) -> tuple[OperationSnapshot, ...]:
        """Quarantine every durable in-flight attempt after a process restart."""
        with self._lock():
            in_flight = tuple(series_id for series_id, series in self._state["series"].items() if series["state"] == "RUNNING")
        for series_id in in_flight:
            self._apply_cleanup(series_id, "CRASH_RECOVERY_QUARANTINE")
        return tuple(self.snapshot(series_id) for series_id in in_flight)

    def _recover(self) -> tuple[OperationSnapshot, ...]:
        recovered: list[OperationSnapshot] = []
        for series_id, series in self._state["series"].items():
            if series["state"] == "RUNNING":
                self._quarantine(series_id, "CRASH_RECOVERY_QUARANTINE")
                recovered.append(self._snapshot(series_id))
        return tuple(recovered)

    def reconcile(self, *, now: int | float) -> tuple[OperationSnapshot, ...]:
        """Quarantine expired Runner sets and reconcile their owning Series."""
        with self._lock():
            expired = tuple((runner["series_id"], runner["ttl_seconds"]) for runner in self._state["runners"].values() if runner["state"] in {"RUNNING", "CLEANUP_UNRESOLVED"} and now >= runner["expires_at"])
        for series_id, _ttl in expired:
            self._apply_cleanup(series_id, "RUNNER_TTL_EXPIRED")
        with self._lock():
            reconciled = self._reconcile(now)
            if reconciled:
                return reconciled
            return tuple(self._snapshot(series_id) for series_id, _ttl in expired)

    def _reconcile(self, now: int | float) -> tuple[OperationSnapshot, ...]:
        reconciled: list[OperationSnapshot] = []
        for resource_id, runner in tuple(self._state["runners"].items()):
            if runner["state"] in {"RUNNING", "CLEANUP_UNRESOLVED"} and now >= runner["expires_at"]:
                series_id = runner["series_id"]
                self._quarantine(series_id, "RUNNER_TTL_EXPIRED")
                reconciled.append(self._snapshot(series_id))
        return tuple(reconciled)

    def _apply_cleanup(self, series_id: str, reason_code: str) -> None:
        """Bounded best-effort cleanup; only acknowledged evidence changes Runner state."""
        with self._lock():
            controls = OperationControls(**self._series(series_id)["controls"])
        outcome: dict[str, str] | None = None
        error: list[BaseException] = []
        def invoke() -> None:
            nonlocal outcome
            try:
                outcome = self._executor.cancel(series_id) if hasattr(self._executor, "cancel") else None
            except BaseException as caught:  # cleanup errors are recorded, never hidden
                error.append(caught)
        worker = threading.Thread(target=invoke, daemon=True)
        worker.start()
        worker.join(controls.cleanup_timeout_seconds)
        with self._lock():
            for runner_id, runner in self._state["runners"].items():
                if runner["series_id"] != series_id or runner["state"] in {"DESTROYED", "QUARANTINED"}:
                    continue
                state = outcome.get(runner_id) if outcome else None
                if state == "destroyed":
                    runner.update(state="DESTROYED", evidence="EXECUTOR_CLEANUP_ACKNOWLEDGED", reason_code=None)
                elif state == "quarantined":
                    runner.update(state="QUARANTINED", evidence="EXECUTOR_CLEANUP_ACKNOWLEDGED", reason_code=reason_code)
                else:
                    runner.update(state="CLEANUP_UNRESOLVED", evidence="EXECUTOR_CLEANUP_TIMEOUT" if worker.is_alive() else "EXECUTOR_CLEANUP_UNACKNOWLEDGED", reason_code=reason_code)
            series = self._series(series_id)
            if series["state"] == "RUNNING":
                self._quarantine(series_id, reason_code, write=False)
            self._write()

    def retry_as_new_series(self, series_id: str, *, new_series_id: str) -> OperationSnapshot:
        with self._lock():
            return self._retry_as_new_series(series_id, new_series_id)

    def _retry_as_new_series(self, series_id: str, new_series_id: str) -> OperationSnapshot:
        source = self._series(series_id)
        if source["state"] not in {"BLOCKED", "QUARANTINED", "CANCELLED"}:
            raise InvalidTransition("RETRY_REQUIRES_TERMINAL_NONSELECTIVE_SERIES")
        if not new_series_id or new_series_id in self._state["series"]:
            raise InvalidTransition("NEW_SERIES_ID_REQUIRED")
        spec_data = dict(source["spec"])
        spec_data["series_id"] = new_series_id
        return self._submit(
            _decode_spec(spec_data),
            mode=OperationMode(source["mode"]),
            controls=OperationControls(**source["controls"]),
            submitted_at=self._clock(),
        )

    def _next_series(self, now: int | float) -> str | None:
        for series_id in self._state["queue"]:
            series = self._series(series_id)
            if series["state"] not in _TERMINAL and series["state"] != "QUEUED" and series["state"] != "WAITING_FOR_CAPACITY":
                return None
            if series["state"] == "QUEUED":
                return series_id
            if series["state"] == "WAITING_FOR_CAPACITY":
                return series_id if now >= series["next_attempt_at"] else None
        return None

    def _mark_running(self, series_id: str, execution_cost: int | float, now: int | float) -> None:
        series = self._series(series_id)
        self._update(series, state="RUNNING", reason_code=None, next_attempt_at=None, execution_cost_spent=series["execution_cost_spent"] + execution_cost, reserved_execution_cost=execution_cost)
        self._set_matches(series_id, "RUNNING", only_declared=True)
        resource_id = f"{series_id}:runner-set" # provisional until authoritative telemetry arrives
        ttl = series["controls"]["runner_ttl_seconds"]
        self._state["runners"][resource_id] = {
            "series_id": series_id,
            "state": "RUNNING",
            "ttl_seconds": ttl,
            "started_at": now,
            "expires_at": now + ttl,
        }
        self._write()

    def _record_bundle(self, series_id: str, bundle: ReleaseBundle) -> None:
        series = self._series(series_id)
        publication = self._state["publication"][series_id]
        # Persist the frozen bundle/evidence atomically with its publication stage.
        if series["mode"] == OperationMode.BATCH_LAB.value:
            report = {**bundle.report, "publication_status": "TEST / NOT FOR PUBLICATION"}
            broadcast = {**bundle.broadcast_manifest, "publication_eligible": False, "publication_status": "TEST / NOT FOR PUBLICATION"}
            manifest = {**bundle.artifact_manifest, "publication_status": "TEST / NOT FOR PUBLICATION"}
            bundle = replace(bundle, publication_eligible=False, report=report, broadcast_manifest=broadcast, artifact_manifest=manifest)
        evidence = bundle.evidence_bundle
        self._state["artifacts"][series_id] = {
            "reference": evidence.get("url", f"sandboxer://artifacts/{series_id}/{bundle.bundle_hash}"),
            "bundle_hash": bundle.bundle_hash,
            "evidence_bundle": evidence,
            "evidence_version": evidence.get("version"),
            "label": "TEST / NOT FOR PUBLICATION" if series["mode"] == OperationMode.BATCH_LAB.value else "DRAFT / NOT PUBLISHED",
            "publication_eligible": False if series["mode"] == OperationMode.BATCH_LAB.value else bundle.publication_eligible,
            "release_bundle": asdict(bundle),
            "replay": bundle.replay,
            "report": bundle.report,
            "broadcast_manifest": bundle.broadcast_manifest,
            "artifact_manifest": bundle.artifact_manifest,
        }
        provisioned = {runner for event in bundle.telemetry if event.get("event_type") == "RUNNERS_PROVISIONED" for runner in event.get("runners", ())}
        teardown = {
            runner: event.get("status") for event in bundle.telemetry if event.get("event_type") == "RUNNER_TEARDOWN"
            for runner in event.get("runners", ())
        }
        provisional = f"{series_id}:runner-set"
        self._state["runners"].pop(provisional, None)
        for runner in provisioned:
            status = teardown.get(runner)
            state = "DESTROYED" if status == "destroyed" else "QUARANTINED"
            self._state["runners"][runner] = {
                "series_id": series_id, "state": state,
                "evidence": "RUNNER_TEARDOWN" if status else "RUNNER_TEARDOWN_MISSING",
                "reason_code": None if status == "destroyed" else "TEARDOWN_UNCERTAIN",
            }
        if bundle.terminal_code == "SERIES_COMPLETED":
            self._update(series, state="COMPLETED", reason_code=None)
            self._set_matches(series_id, "NOT_REQUIRED")
            for result in bundle.match_results:
                record = self._state["matches"][f"{series_id}:match-{result['match_number']}"]
                self._update(record, state="COMPLETED", reason_code=result["reason_code"])
            # Completion is never allowed to manufacture destroy evidence.
            if series["mode"] == OperationMode.BATCH_LAB.value:
                self._update(publication, state="TEST_DRAFT_READY", artifact_label="TEST / NOT FOR PUBLICATION")
            elif bundle.publication_eligible:
                self._update(publication, state="AWAITING_PUBLICATION_APPROVAL")
            else:
                self._update(publication, state="BLOCKED")
        else:
            self._quarantine(series_id, bundle.terminal_code, write=False)
        self._write()

    def _block(self, series_id: str, reason_code: str) -> None:
        series = self._series(series_id)
        self._update(series, state="BLOCKED", reason_code=reason_code, next_attempt_at=None)
        self._update(self._state["publication"][series_id], state="BLOCKED")
        self._set_matches(series_id, "BLOCKED", only_declared=True)
        self._write()

    def _quarantine(self, series_id: str, reason_code: str, *, write: bool = True) -> None:
        series = self._series(series_id)
        self._update(series, state="QUARANTINED", reason_code=reason_code, next_attempt_at=None)
        self._update(self._state["publication"][series_id], state="QUARANTINED")
        self._set_matches(series_id, "QUARANTINED", only_declared=False)
        self._quarantine_runners(series_id, reason_code)
        if write:
            self._write()

    def _quarantine_runners(self, series_id: str, reason_code: str) -> None:
        for record in self._state["runners"].values():
            if record["series_id"] == series_id and record["state"] not in {"DESTROYED", "CLEANUP_UNRESOLVED"}:
                record["state"] = "QUARANTINED"
                record["reason_code"] = reason_code

    def _set_runner_state(self, series_id: str, state: str) -> None:
        for record in self._state["runners"].values():
            if record["series_id"] == series_id:
                record["state"] = state

    def _set_matches(self, series_id: str, state: str, *, only_declared: bool = False) -> None:
        for record in self._state["matches"].values():
            if record["series_id"] == series_id and (not only_declared or record["state"] == "DECLARED"):
                self._update(record, state=state)

    def _series(self, series_id: str) -> dict[str, object]:
        try:
            return self._state["series"][series_id]
        except KeyError as error:
            raise KeyError(f"unknown Series: {series_id}") from error

    @staticmethod
    def _require_revision(record: dict[str, object], expected_revision: int) -> None:
        if record["revision"] != expected_revision:
            raise StaleRevision("COMPARE_AND_SET_CONFLICT")

    @staticmethod
    def _update(record: dict[str, object], **changes: object) -> None:
        record.update(changes)
        record["revision"] = int(record["revision"]) + 1

    def _write(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(self._path.suffix + ".tmp")
        temporary.write_text(json.dumps(self._state, sort_keys=True), encoding="utf-8")
        temporary.replace(self._path)


def _decode_spec(data: dict[str, object]) -> SeriesSpec:
    competitors = tuple(
        ControlledCompetitor(
            public_name=entry["public_name"],
            adapter=FakeModelAdapter(**entry["adapter"]),
        )
        for entry in data["competitors"]
    )
    return SeriesSpec(
        schema_version=data["schema_version"],
        series_id=data["series_id"],
        seed=data["seed"],
        competitors=competitors,
        match_policy=MatchPolicy(**data["match_policy"]),
        runner_backend=FakeRunnerBackend(**data["runner_backend"]),
        clock=ControlledClock(**data["clock"]),
        command_code_credit_allowance=data.get("command_code_credit_allowance"),
        minimum_simulated_duration_seconds=data.get("minimum_simulated_duration_seconds"),
        runner_image=str(data.get("runner_image", "sandboxer-runner:v1")),
        runner_base_image_digest=str(data.get("runner_base_image_digest", "7f83b1657ff1fc53b92dc18148a1d65dfc2d4b1fa3d677284addd200126d9069")),
        mcp_bridge_version=str(data.get("mcp_bridge_version", "sandboxer.mcp-bridge.v1")),
        arena_config_digest=str(data["arena_config_digest"]) if data.get("arena_config_digest") is not None else None,
        is_calibration=bool(data.get("is_calibration", False)),
        publication_enabled=bool(data.get("publication_enabled", True)),
    )
