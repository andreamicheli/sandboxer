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
from .evidence import EvidenceBundle, EvidenceFreezeError, EvidenceVersionStore, freeze_evidence_bundle, verify_evidence_bundle
from .replay import ReplayError, ReplayRenderer, build_replay, render_replay, sanitize_terminal_text
from .report import ReportModel, ResultReport, ResultReportError, build_result_report, generate_result_report, render_report_html, render_report_pdf
from .local_kvm import LocalKvmConfig, LocalKvmRunnerProvider, SubprocessLocalKvmHost
from .runner_backend import ProvisioningFailed
from .operations import (
    CapacityCondition,
    CapacityObservation,
    ExecutionResult,
    InvalidTransition,
    OperationControls,
    OperationMode,
    OperationSnapshot,
    PublicationBlocked,
    SeriesOperations,
    StaleRevision,
)
from .command_code import CommandCodeAdapter, CommandCodeError, CommandCodePreflight, CommandCodeResult

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
    "EvidenceBundle",
    "EvidenceFreezeError",
    "EvidenceVersionStore",
    "freeze_evidence_bundle",
    "verify_evidence_bundle",
    "ReplayError",
    "ReplayRenderer",
    "build_replay",
    "render_replay",
    "sanitize_terminal_text",
    "ResultReport",
    "ReportModel",
    "ResultReportError",
    "build_result_report",
    "generate_result_report",
    "render_report_html",
    "render_report_pdf",
    "LocalKvmConfig",
    "LocalKvmRunnerProvider",
    "SubprocessLocalKvmHost",
    "ProvisioningFailed",
    "CapacityCondition",
    "CapacityObservation",
    "ExecutionResult",
    "InvalidTransition",
    "OperationControls",
    "OperationMode",
    "OperationSnapshot",
    "PublicationBlocked",
    "SeriesOperations",
    "StaleRevision",
    "CommandCodeAdapter",
    "CommandCodeError",
    "CommandCodePreflight",
    "CommandCodeResult",
]
