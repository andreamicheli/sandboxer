"""Deterministic v0 dry-run seam: ``SeriesSpec -> ReleaseBundle``.

This module is deliberately credential-free.  Its fake ports make the public
control-plane contract executable before a provider or production Runner is
connected.
"""

from .series import (
    ControlledCompetitor,
    ControlledClock,
    FakeModelAdapter,
    FakeRunnerBackend,
    MatchPolicy,
    ReleaseBundle,
    SeriesSpec,
    execute_series,
)
from .arena_safety import (
    ArenaNetworkPolicy,
    NetworkExpectation,
    NetworkObservation,
    NetworkValidation,
    Phase,
    ReconciliationLedger,
    TeardownEvidence,
    TeardownState,
)
from .auditor import (
    AdvisoryRecommendation,
    AuditAction,
    AuditFinding,
    AuditReasonCode,
    AuditVerdict,
    Auditor,
    RedactedProjection,
    audit_series,
)
from .local_kvm import LocalKvmConfig, LocalKvmRunnerProvider, SubprocessLocalKvmHost
from .runner_backend import ProvisioningFailed

__all__ = [
    "ControlledCompetitor",
    "ControlledClock",
    "FakeModelAdapter",
    "FakeRunnerBackend",
    "MatchPolicy",
    "ReleaseBundle",
    "SeriesSpec",
    "execute_series",
    "ArenaNetworkPolicy",
    "NetworkExpectation",
    "NetworkObservation",
    "NetworkValidation",
    "Phase",
    "ReconciliationLedger",
    "TeardownEvidence",
    "TeardownState",
    "AdvisoryRecommendation",
    "AuditAction",
    "AuditFinding",
    "AuditReasonCode",
    "AuditVerdict",
    "Auditor",
    "RedactedProjection",
    "audit_series",
    "LocalKvmConfig",
    "LocalKvmRunnerProvider",
    "SubprocessLocalKvmHost",
    "ProvisioningFailed",
]
