#!/usr/bin/env python3
"""Run a real, credential-free two-Runner local Docker rehearsal (no KVM)."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path


# This script is an operational entry point, so it must not depend on the
# caller's current directory or a manually exported PYTHONPATH.
PILOT_ROOT = Path(__file__).resolve().parents[1]
if str(PILOT_ROOT) not in sys.path:
    sys.path.insert(0, str(PILOT_ROOT))

from sandboxer_v0.local_docker import LocalDockerConfig, LocalDockerRunnerProvider
from sandboxer_v0.runner_backend import (
    ProductionRunnerBackend,
    ProvisioningFailed,
    RunnerPreflightFailed,
    TeardownUncertain,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--image-digest", required=True,
                        help="pinned sha256 hex of the image (docker image inspect)")
    parser.add_argument("--runner-root", type=Path, default=Path("/tmp/sandboxer-docker-runners"))
    parser.add_argument("--match-id", required=True)
    parser.add_argument("--ttl-seconds", type=int, default=180)
    parser.add_argument("--evidence-path", type=Path)
    arguments = parser.parse_args(argv)
    config = LocalDockerConfig(
        runner_root=arguments.runner_root.resolve(),
        image=arguments.image,
        image_digest=arguments.image_digest,
        toy_service_port=8080,
        ttl_seconds=arguments.ttl_seconds,
    )
    backend = ProductionRunnerBackend(LocalDockerRunnerProvider(config))
    evidence_path = arguments.evidence_path or Path(f"/tmp/{arguments.match_id}.docker-rehearsal.json")
    try:
        report = backend.rehearse(match_id=arguments.match_id, runner_names=("atlas", "borealis"))
    except (ProvisioningFailed, RunnerPreflightFailed, TeardownUncertain) as error:
        payload = {
            "result": "failed",
            "reason_code": error.reason_code,
            "production_ready": False,
            "active_witness_summary": "unavailable-after-failed-preflight",
            "teardown": [_safe_teardown(item) for item in error.teardown_evidence],
        }
        _write_evidence(evidence_path, payload)
        print(json.dumps(payload, indent=2), file=sys.stderr)
        return 2
    payload = {
        "result": "passed", "terminal_code": report.terminal_code,
        "production_ready": report.production_ready,
        "active_witness_summary": "blue-active-witnesses-passed; red-policy-prepared-by-provider",
        "preflight_checks": [{"name": check.name, "passed": check.passed, "reason_code": check.reason_code} for check in report.preflight_checks],
        "teardown": [_safe_teardown(item) for item in report.teardown_evidence],
    }
    _write_evidence(evidence_path, payload)
    print(json.dumps(payload, indent=2))
    return 0


def _safe_teardown(item: object) -> dict[str, str | None]:
    """Keep only typed lifecycle fields; never serialize paths or raw logs."""
    data = asdict(item)
    return {key: str(data[key]) if data[key] is not None else None for key in ("resource_id", "state", "reason_code", "evidence")}


def _write_evidence(path: Path, payload: dict[str, object]) -> None:
    """Durably publish a bounded, typed report before any stdout consumer."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    encoded = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    if len(encoded) > 64 * 1024:
        raise RuntimeError("REHEARSAL_EVIDENCE_TOO_LARGE")
    descriptor, temporary = tempfile.mkstemp(prefix=".rehearsal-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(encoded + b"\n")
            output.flush(); os.fsync(output.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


if __name__ == "__main__":  # pragma: no cover - CLI is exercised against the daemon
    raise SystemExit(main())
