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
]
