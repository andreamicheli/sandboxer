"""Invariants of the LocalKvm QEMU argv builder (defensive security audit).

These tests exercise only the argv builder; QEMU is never launched.
Findings and rationale live in docs/security-audit-2026-08-23.md.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sandboxer_v0.local_kvm import LocalKvmConfig, LocalKvmRunnerProvider


def hardened_provider(tmp_path: Path) -> LocalKvmRunnerProvider:
    config = LocalKvmConfig(
        runner_root=tmp_path / "runners",
        base_image=tmp_path / "immutable-base.qcow2",
        base_image_sha256="0" * 64,
        base_profile=tmp_path / "immutable-base.profile.json",
        qemu_user="sandboxer-runner",
        qemu_uid=1001,
        qemu_gid=1001,
        toy_service_port=8080,
    )
    return LocalKvmRunnerProvider(config)


def qemu_argv(tmp_path: Path) -> tuple[str, ...]:
    provider = hardened_provider(tmp_path)
    return provider._qemu_command(  # noqa: SLF001 - audit inspects the builder
        namespace="sbb-deadbeef",
        tap="tap-deadbeef",
        workspace=tmp_path / "workspace.qcow2",
        seed=tmp_path / "seed.iso",
        control_socket=tmp_path / "control.sock",
        serial_log=tmp_path / "serial.log",
        mac_address="02:00:00:00:00:11",
    )


def test_no_guest_reachable_port_forwarding(tmp_path: Path) -> None:
    argv = qemu_argv(tmp_path)
    assert "hostfwd" not in " ".join(argv)
    assert "-netdev" in argv
    netdevs = [item for item in argv if item.startswith("tap,") or item.startswith("user,")]
    assert len(netdevs) == 1
    assert all("hostfwd=" not in item for item in netdevs)


def test_memory_flag_present(tmp_path: Path) -> None:
    argv = qemu_argv(tmp_path)
    assert "-m" in argv
    index = argv.index("-m")
    declared = int(argv[index + 1])
    assert declared == hardened_provider(tmp_path).config.memory_mib
    assert declared >= 256


def test_guest_nic_is_confined_and_readonly_base(tmp_path: Path) -> None:
    argv = qemu_argv(tmp_path)
    assert "-nic" in argv and argv[argv.index("-nic") + 1] == "none"
    assert any(item == "tap,id=arena0,ifname=tap-deadbeef,script=no,downscript=no" for item in argv)
    assert any(item.startswith(f"file={tmp_path}/immutable-base.qcow2,if=virtio,format=qcow2,readonly=on") for item in argv)
    assert all("-virtfs" not in item for item in argv)
    assert "9p" not in " ".join(argv) and "virtiofs" not in " ".join(argv)


@pytest.mark.xfail(
    reason="GAP-1: TAP-in-netns is used instead of user-mode slirp "
    "(accepted deviation, docs/security-audit-2026-08-23.md)",
    strict=True,
)
def test_user_mode_networking_used(tmp_path: Path) -> None:
    """Literal slirp invariant: currently fails by design (GAP-1).

    strict=True forces re-triage of this check whenever the netdev type
    changes; see docs/security-audit-2026-08-23.md GAP-1.
    """
    argv = qemu_argv(tmp_path)
    assert any(item.startswith("user,") for item in argv)
