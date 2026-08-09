#!/usr/bin/env python3
"""Fail-closed checks for the local pilot before an agent can start."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=ROOT, text=True, capture_output=True, check=False)


def admin_members() -> set[str]:
    result = run("dscl", ".", "-read", "/Groups/admin", "GroupMembership")
    if result.returncode != 0:
        return set()
    return set(result.stdout.partition(":")[2].split())


def compose_model() -> dict[str, object]:
    result = run("docker", "compose", "config", "--format", "json")
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "docker compose config failed")
    return json.loads(result.stdout)


def validate_compose(model: dict[str, object]) -> list[str]:
    failures: list[str] = []
    services = model.get("services", {})
    if not isinstance(services, dict):
        return ["compose services missing"]

    for name in ("alpha", "beta"):
        service = services.get(name, {})
        if not isinstance(service, dict):
            failures.append(f"{name}: missing")
            continue
        if service.get("privileged") is True:
            failures.append(f"{name}: privileged")
        if str(service.get("user", "")).split(":", 1)[0] in ("", "0", "root"):
            failures.append(f"{name}: explicit non-root user missing")
        if service.get("read_only") is not True:
            failures.append(f"{name}: root filesystem not read-only")
        if "ALL" not in service.get("cap_drop", []):
            failures.append(f"{name}: capabilities not dropped")
        if not any("no-new-privileges" in value for value in service.get("security_opt", [])):
            failures.append(f"{name}: no-new-privileges missing")
        if service.get("network_mode") == "host":
            failures.append(f"{name}: host network")
        if service.get("ports"):
            failures.append(f"{name}: host ports exposed")
        if any(network == "egress" for network in service.get("networks", {})):
            failures.append(f"{name}: direct egress network")
        if not service.get("pids_limit") or not service.get("mem_limit") or not service.get("cpus"):
            failures.append(f"{name}: resource limit missing")
        for mount in service.get("volumes", []):
            if isinstance(mount, dict) and mount.get("type") == "bind":
                failures.append(f"{name}: host bind mount {mount.get('source')}")

    alpha = services.get("alpha", {})
    beta = services.get("beta", {})
    if isinstance(alpha, dict) and isinstance(beta, dict):
        shared = set(alpha.get("networks", {})) & set(beta.get("networks", {}))
        if shared:
            failures.append(f"blue phase: runners share networks {sorted(shared)}")

    networks = model.get("networks", {})
    if not isinstance(networks, dict):
        failures.append("networks missing")
    else:
        for name in ("alpha_private", "beta_private"):
            network = networks.get(name, {})
            if not isinstance(network, dict) or network.get("internal") is not True:
                failures.append(f"{name}: network is not internal")
    return failures


def validate_colima(config: dict[str, object]) -> list[str]:
    failures: list[str] = []
    if config.get("mounts") not in (None, []):
        failures.append("host mounts are enabled")
    if config.get("portForwarder") != "none":
        failures.append("port forwarding is enabled")
    if config.get("vmType") != "vz":
        failures.append("unexpected VM type")
    if config.get("runtime") != "docker":
        failures.append("unexpected container runtime")
    return failures


def main() -> int:
    checks: list[tuple[str, bool, str]] = []
    username = os.environ.get("USER", "")
    checks.append(("standard macOS user", username not in admin_members(), username))
    checks.append(("workspace is user-owned", ROOT.stat().st_uid == os.getuid(), str(ROOT)))

    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    mode = config.get("model_mode")
    provider_calls = config.get("allow_provider_calls")
    gate_coherent = (mode == "disabled" and provider_calls is False) or (mode == "groq" and provider_calls is True)
    checks.append(("provider gate is coherent", gate_coherent, f"mode={mode!r}, calls={provider_calls!r}"))

    context = run("docker", "context", "show")
    checks.append(("docker context is Colima profile", context.stdout.strip() == "colima-sandboxer", context.stdout.strip()))

    colima_path = Path.home() / ".colima/sandboxer/colima.yaml"
    try:
        colima_failures = validate_colima(yaml.safe_load(colima_path.read_text(encoding="utf-8")))
    except Exception as error:
        colima_failures = [str(error)]
    checks.append(("Colima host boundary", not colima_failures, "; ".join(colima_failures) or "ok"))

    try:
        failures = validate_compose(compose_model())
    except Exception as error:  # fail closed and report the concrete cause
        failures = [str(error)]
    checks.append(("compose isolation contract", not failures, "; ".join(failures) or "ok"))

    for label, passed, detail in checks:
        print(f"{'PASS' if passed else 'FAIL'}  {label}: {detail}")
    return 0 if all(passed for _, passed, _ in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
