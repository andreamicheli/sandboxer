from __future__ import annotations

from sandboxer_v0.local_kvm_watchdog import main


def test_watchdog_rejects_invalid_or_missing_argv_without_a_shell() -> None:
    assert main([]) == 2
    assert main(["not-a-pid", "1", "/tmp/not-a-cgroup"]) == 2
