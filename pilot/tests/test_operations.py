from __future__ import annotations

import json
import threading

import pytest

from sandboxer_v0 import (
    CapacityCondition,
    CapacityObservation,
    ControlledCompetitor,
    FakeModelAdapter,
    FakeRunnerBackend,
    MatchPolicy,
    OperationControls,
    OperationMode,
    PublicationBlocked,
    SeriesOperations,
    SeriesSpec,
)


def _spec(series_id: str) -> SeriesSpec:
    return SeriesSpec(
        schema_version="sandboxer.series-spec.v1",
        series_id=series_id,
        seed="operation-seed",
        competitors=(
            ControlledCompetitor("atlas", FakeModelAdapter("fake/atlas", ("defend", "capture"))),
            ControlledCompetitor("borealis", FakeModelAdapter("fake/borealis", ("defend", "finish"))),
        ),
        match_policy=MatchPolicy(best_of=3, output_token_budget=100, turn_budget=2, tool_budget=2),
        runner_backend=FakeRunnerBackend(),
    )


class _Availability:
    def __init__(self, *conditions: CapacityObservation) -> None:
        self._conditions = list(conditions)

    def observe(self, _spec: SeriesSpec) -> CapacityObservation:
        return self._conditions.pop(0) if self._conditions else CapacityObservation(CapacityCondition.AVAILABLE)


def test_supervised_series_requires_separate_spend_and_publication_approvals(tmp_path) -> None:
    operations = SeriesOperations(tmp_path / "operations.json")
    submitted = operations.submit(_spec("supervised-001"), mode=OperationMode.SUPERVISED)

    assert submitted.series_state == "AWAITING_SPEND_APPROVAL"
    assert submitted.publication_state == "DRAFT"
    assert operations.advance(now=0) == ()

    approved = operations.approve_spend("supervised-001", expected_revision=submitted.series_revision)
    completed, = operations.advance(now=1)

    assert approved.series_state == "QUEUED"
    assert completed.series_state == "COMPLETED"
    assert completed.publication_state == "AWAITING_PUBLICATION_APPROVAL"
    assert completed.publication_pointer is None
    assert all(match["state"] in {"COMPLETED", "NOT_REQUIRED"} for match in completed.matches)

    published = operations.approve_publication(
        "supervised-001", expected_revision=completed.publication_revision
    )

    assert published.publication_state == "PUBLISHED"
    assert published.publication_pointer == "sandboxer://publication/supervised-001"


def test_batch_lab_serially_waits_then_generates_only_test_drafts(tmp_path) -> None:
    availability = _Availability(
        CapacityObservation(CapacityCondition.RECOVERABLE_CAPACITY),
        CapacityObservation(CapacityCondition.AVAILABLE),
        CapacityObservation(CapacityCondition.AVAILABLE),
    )
    operations = SeriesOperations(tmp_path / "operations.json", availability=availability)
    controls = OperationControls(wait_deadline_seconds=30, initial_backoff_seconds=2, max_backoff_seconds=8)
    operations.submit(_spec("batch-001"), mode=OperationMode.BATCH_LAB, controls=controls)
    operations.submit(_spec("batch-002"), mode=OperationMode.BATCH_LAB, controls=controls)

    waiting, = operations.advance(now=0)
    assert waiting.series_id == "batch-001"
    assert waiting.series_state == "WAITING_FOR_CAPACITY"
    assert waiting.next_attempt_at == 2
    assert operations.advance(now=1) == ()

    first, = operations.advance(now=2)
    second, = operations.advance(now=3)

    assert first.series_state == second.series_state == "COMPLETED"
    assert first.publication_state == second.publication_state == "TEST_DRAFT_READY"
    assert first.artifact_label == "TEST / NOT FOR PUBLICATION"
    assert second.publication_pointer is None
    with pytest.raises(PublicationBlocked, match="BATCH_LAB_PUBLICATION_FORBIDDEN"):
        operations.approve_publication("batch-001", expected_revision=first.publication_revision)


@pytest.mark.parametrize(
    ("observation", "expected"),
    [
        (CapacityObservation(CapacityCondition.AUTH_FAILURE), "AUTH_FAILURE"),
        (CapacityObservation(CapacityCondition.CATALOG_DRIFT), "CATALOG_DRIFT"),
        (CapacityObservation(CapacityCondition.POLICY_INCOMPATIBLE), "POLICY_INCOMPATIBLE"),
        (CapacityObservation(CapacityCondition.SAFETY_FAILURE), "SAFETY_FAILURE"),
    ],
)
def test_nonrecoverable_provider_or_safety_conditions_block_without_waiting(tmp_path, observation, expected) -> None:
    operations = SeriesOperations(tmp_path / "operations.json", availability=_Availability(observation))
    operations.submit(_spec("blocked-001"), mode=OperationMode.BATCH_LAB)

    blocked, = operations.advance(now=0)

    assert blocked.series_state == "BLOCKED"
    assert blocked.reason_code == expected
    assert blocked.wait_attempts == 0


def test_waiting_obeys_deadline_and_cost_ceiling(tmp_path) -> None:
    availability = _Availability(CapacityObservation(CapacityCondition.RECOVERABLE_CREDITS, wait_cost=2))
    operations = SeriesOperations(tmp_path / "operations.json", availability=availability)
    controls = OperationControls(
        wait_deadline_seconds=10,
        initial_backoff_seconds=2,
        max_backoff_seconds=2,
        wait_cost_ceiling=1,
    )
    operations.submit(_spec("ceiling-001"), mode=OperationMode.BATCH_LAB, controls=controls)

    blocked, = operations.advance(now=0)

    assert blocked.series_state == "BLOCKED"
    assert blocked.reason_code == "WAIT_COST_CEILING_EXCEEDED"


def test_retry_is_a_new_series_attempt_with_a_new_identity_and_same_declared_policy(tmp_path) -> None:
    operations = SeriesOperations(tmp_path / "operations.json")
    operations.submit(
        SeriesSpec(**{**_spec("failed-001").__dict__, "runner_backend": FakeRunnerBackend(teardown="uncertain")}),
        mode=OperationMode.BATCH_LAB,
    )
    failed, = operations.advance(now=0)

    retried = operations.retry_as_new_series("failed-001", new_series_id="failed-001-attempt-2")

    assert failed.series_state == "QUARANTINED"
    assert retried.series_id == "failed-001-attempt-2"
    assert retried.match_policy == {"best_of": 3, "output_token_budget": 100, "turn_budget": 2, "tool_budget": 2}
    assert {match["match_id"] for match in retried.matches} == {
        "failed-001-attempt-2:match-1",
        "failed-001-attempt-2:match-2",
        "failed-001-attempt-2:match-3",
    }


def test_cancellation_and_crash_recovery_quarantine_tracked_runner_set(tmp_path) -> None:
    state_path = tmp_path / "operations.json"

    def interrupted(_spec: SeriesSpec):
        raise KeyboardInterrupt("simulated crash")

    operations = SeriesOperations(state_path, executor=interrupted)
    submitted = operations.submit(_spec("recovery-001"), mode=OperationMode.BATCH_LAB)
    with pytest.raises(KeyboardInterrupt, match="simulated crash"):
        operations.advance(now=0)

    recovered = SeriesOperations(state_path).recover()
    snapshot = recovered[0]
    persisted = json.loads(state_path.read_text())

    assert submitted.series_state == "QUEUED"
    assert snapshot.series_state == "QUARANTINED"
    assert snapshot.reason_code == "CRASH_RECOVERY_QUARANTINE"
    assert snapshot.tracked_runners == ("recovery-001:runner-set",)
    assert persisted["runners"]["recovery-001:runner-set"]["state"] == "QUARANTINED"

    cancelled = SeriesOperations(tmp_path / "cancel.json")
    queued = cancelled.submit(_spec("cancel-001"), mode=OperationMode.BATCH_LAB)
    terminal = cancelled.cancel("cancel-001", expected_revision=queued.series_revision)
    assert terminal.series_state == "CANCELLED"
    assert terminal.reason_code == "OPERATOR_CANCELLED"


def test_compare_and_set_rejects_stale_approval_and_ttl_reconciliation_is_durable(tmp_path) -> None:
    state_path = tmp_path / "operations.json"

    def interrupted(_spec: SeriesSpec):
        raise KeyboardInterrupt("simulated crash")

    operations = SeriesOperations(state_path, executor=interrupted)
    submitted = operations.submit(
        _spec("ttl-001"),
        mode=OperationMode.SUPERVISED,
        controls=OperationControls(runner_ttl_seconds=10),
    )
    approved = operations.approve_spend("ttl-001", expected_revision=submitted.series_revision)
    with pytest.raises(Exception, match="COMPARE_AND_SET_CONFLICT"):
        operations.approve_spend("ttl-001", expected_revision=submitted.series_revision)
    with pytest.raises(KeyboardInterrupt):
        operations.advance(now=5)

    reconciled, = SeriesOperations(state_path).reconcile(now=15)
    state = json.loads(state_path.read_text())

    assert approved.series_state == "QUEUED"
    assert reconciled.reason_code == "RUNNER_TTL_EXPIRED"
    assert state["series"]["ttl-001"]["state"] == "QUARANTINED"
    assert state["matches"]["ttl-001:match-1"]["state"] == "QUARANTINED"
    assert state["publication"]["ttl-001"]["state"] == "QUARANTINED"


def test_in_flight_cancellation_prevents_late_executor_completion_from_overwriting_terminal_state(tmp_path) -> None:
    started, release = threading.Event(), threading.Event()

    class BlockingExecutor:
        def __call__(self, spec):
            started.set()
            release.wait(timeout=2)
            from sandboxer_v0 import execute_series
            return execute_series(spec)

        def cancel(self, _series_id):
            release.set()

    path = tmp_path / "operations.json"
    operations = SeriesOperations(path, executor=BlockingExecutor())
    queued = operations.submit(_spec("inflight-001"), mode=OperationMode.BATCH_LAB)
    worker = threading.Thread(target=lambda: operations.advance(now=0))
    worker.start()
    assert started.wait(timeout=1)
    concurrent_operator = SeriesOperations(path, executor=BlockingExecutor())
    cancelled = concurrent_operator.cancel(
        "inflight-001", expected_revision=concurrent_operator.snapshot("inflight-001").series_revision
    )
    worker.join(timeout=2)

    assert cancelled.series_state == "CANCELLED"
    assert SeriesOperations(path).snapshot("inflight-001").series_state == "CANCELLED"
