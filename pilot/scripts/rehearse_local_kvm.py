#!/usr/bin/env python3
"""Run a real, credential-free two-Runner local KVM rehearsal as root."""

from __future__ import annotations

import argparse
import json
import os
import pwd
import sys
from dataclasses import asdict
from pathlib import Path


# This script is an operational entry point, so it must not depend on the
# caller's current directory or a manually exported PYTHONPATH.
PILOT_ROOT = Path(__file__).resolve().parents[1]
if str(PILOT_ROOT) not in sys.path:
    sys.path.insert(0, str(PILOT_ROOT))

from sandboxer_v0.local_kvm import LocalKvmConfig, LocalKvmRunnerProvider
from sandboxer_v0.runner_backend import (
    ProductionRunnerBackend,
    ProvisioningFailed,
    RunnerPreflightFailed,
    TeardownUncertain,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--runner-root", type=Path, default=Path("/var/lib/sandboxer/runners"))
    parser.add_argument("--match-id", required=True)
    parser.add_argument("--ttl-seconds", type=int, default=180)
    arguments = parser.parse_args(argv)
    if os.geteuid() != 0:
        print("LOCAL_KVM_REHEARSAL_REQUIRES_ROOT", file=sys.stderr)
        return 2
    account = pwd.getpwnam("sandboxer-runner")
    config = LocalKvmConfig(
        runner_root=arguments.runner_root.resolve(),
        base_image=arguments.image.resolve(),
        base_image_sha256=_sha256(arguments.image),
        base_profile=arguments.profile.resolve(),
        qemu_user="sandboxer-runner",
        qemu_uid=account.pw_uid,
        toy_service_port=8080,
        ttl_seconds=arguments.ttl_seconds,
    )
    backend = ProductionRunnerBackend(LocalKvmRunnerProvider(config))
    try:
        report = backend.rehearse(match_id=arguments.match_id, runner_names=("atlas", "borealis"))
    except (ProvisioningFailed, RunnerPreflightFailed, TeardownUncertain) as error:
        print(json.dumps({
            "result": "failed",
            "reason_code": error.reason_code,
            "teardown_evidence": [asdict(item) for item in error.teardown_evidence],
        }, indent=2, default=str), file=sys.stderr)
        return 2
    print(json.dumps(asdict(report), indent=2, default=str))
    return 0


def _sha256(path: Path) -> str:
    import hashlib

    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


if __name__ == "__main__":  # pragma: no cover - CLI is exercised on the KVM host
    raise SystemExit(main())
