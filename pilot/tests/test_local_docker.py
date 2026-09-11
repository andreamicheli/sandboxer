"""Unit tests for the no-KVM Docker Runner provider (fake host, no daemon)."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sandboxer_v0.arena_safety import Phase, TeardownState  # noqa: E402
from sandboxer_v0.local_docker import (  # noqa: E402
    CommandResult,
    LocalDockerConfig,
    LocalDockerRunnerProvider,
)
from sandboxer_v0.runner_backend import ProductionRunnerBackend  # noqa: E402

DIGEST = "a" * 64
IMAGE = "sandboxer-runner:test"


def _config(tmp_path: Path, **overrides) -> LocalDockerConfig:
    values: dict = {
        "runner_root": tmp_path / "runners",
        "image": IMAGE,
        "image_digest": DIGEST,
        "ttl_seconds": 300,
    }
    values.update(overrides)
    return LocalDockerConfig(**values)


class FakeDockerHost:
    """Argv-recording fake; replays canned docker CLI outputs."""

    def __init__(self) -> None:
        self.argv: list[tuple[str, ...]] = []
        self.running: dict[str, bool] = {}
        self.destroy_fail: set[str] = set()
        self.networks: dict[str, set[str]] = {}
        self.counter = 0

    def run(self, argv: tuple[str, ...], *, timeout_seconds: float = 30) -> CommandResult:
        self.argv.append(argv)
        verb = argv[0]
        if verb == "image":
            return CommandResult(0, f"{IMAGE}@sha256:{DIGEST} sha256:{DIGEST}\n", "")
        if verb == "network" and argv[1] == "create":
            return CommandResult(0, "netid\n", "")
        if verb == "run":
            self.counter += 1
            name = argv[argv.index("--name") + 1]
            self.running[name] = True
            return CommandResult(0, f"{self.counter:012x}deadbeefcafe\n", "")
        if verb == "inspect" and "--format" in argv:
            name = argv[1]
            return CommandResult(0, "true\n" if self.running.get(name) else "\n", "")
        if verb == "inspect":
            name = argv[1]
            if not self.running.get(name):
                return CommandResult(1, "", "No such object")
            return CommandResult(0, json.dumps([_state(name, {"none": {}})]), "")
        if verb == "stop":
            return CommandResult(0, "", "")
        if verb == "rm":
            name = argv[-1]
            if name in self.destroy_fail:
                return CommandResult(0, "", "")
            self.running.pop(name, None)
            return CommandResult(0, name + "\n", "")
        if verb == "network":
            return CommandResult(0, "", "")
        if verb == "ps":
            return CommandResult(0, "", "")
        raise AssertionError(f"unexpected argv: {argv}")


def _state(name: str, networks: dict) -> dict:
    return {
        "Id": "cid-" + name,
        "State": {"Running": True, "StartedAt": "2026-09-12T00:00:00Z"},
        "Config": {"User": "10001"},
        "HostConfig": {
            "ReadonlyRootfs": True,
            "Memory": 512 * 1024 * 1024,
            "PidsLimit": 128,
        },
        "Mounts": [{"Type": "tmpfs", "Destination": "/arena"}],
        "NetworkSettings": {"Networks": networks},
    }


class ArenaAwareHost(FakeDockerHost):
    """Fake that tracks `network connect` so RED witnesses pass."""

    def __init__(self) -> None:
        super().__init__()
        self.attached: dict[str, set[str]] = {}
        self.detached: set[str] = set()

    def run(self, argv: tuple[str, ...], *, timeout_seconds: float = 30) -> CommandResult:
        if argv[0] == "network" and argv[1] == "disconnect":
            _, _, _network, container = argv
            self.detached.add(container)
            self.argv.append(argv)
            return CommandResult(0, "", "")
        if argv[0] == "network" and argv[1] == "connect":
            _, _, network, container = argv
            self.attached.setdefault(container, set()).add(network)
            self.argv.append(argv)
            return CommandResult(0, "", "")
        if argv[0] == "inspect" and "--format" not in argv:
            name = argv[1]
            if not self.running.get(name):
                self.argv.append(argv)
                return CommandResult(1, "", "No such object")
            nets = {} if name in self.detached else {"none": {}}
            for net in self.attached.get(name, set()):
                nets[net] = {}
            self.argv.append(argv)
            return CommandResult(0, json.dumps([_state(name, nets)]), "")
        return super().run(argv, timeout_seconds=timeout_seconds)


def test_config_bounds(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _config(tmp_path, memory_mib=128)
    with pytest.raises(ValueError):
        _config(tmp_path, pids_max=8)
    with pytest.raises(ValueError):
        _config(tmp_path, ttl_seconds=5)
    with pytest.raises(ValueError):
        _config(tmp_path, image_digest="not-a-digest")
    with pytest.raises(ValueError):
        _config(tmp_path, cpus=2.0)


def test_run_argv_hardening(tmp_path: Path) -> None:
    host = ArenaAwareHost()
    provider = LocalDockerRunnerProvider(_config(tmp_path), host=host)
    runners = provider.provision("m1", ("atlas", "borealis"))
    assert len(runners) == 2
    runs = [argv for argv in host.argv if argv[0] == "run"]
    assert len(runs) == 2
    for argv in runs:
        text = " ".join(argv)
        assert "--network none" in text
        assert "--read-only" in text
        assert "--cap-drop=ALL" in text
        assert "no-new-privileges:true" in text
        assert "--user 10001" in text
        assert "--pids-limit 128" in text
        assert "--privileged" not in text
        assert " -p " not in text and "--publish" not in text
        assert ":/" not in text or "--tmpfs" in text  # no bind mounts
        assert "--volume" not in text and "-v " not in text
    # Kernel IDs are distinct containers on a shared host kernel.
    assert runners[0].kernel_id != runners[1].kernel_id
    assert runners[0].kernel_id.startswith("docker:")
    for runner in runners:
        provider.destroy(runner)


def test_digest_mismatch_fails_closed(tmp_path: Path) -> None:
    class WrongDigest(FakeDockerHost):
        def run(self, argv: tuple[str, ...], *, timeout_seconds: float = 30) -> CommandResult:
            if argv[0] == "image":
                return CommandResult(0, "other@sha256:" + "b" * 64 + "\n", "")
            return super().run(argv, timeout_seconds=timeout_seconds)

    provider = LocalDockerRunnerProvider(_config(tmp_path), host=WrongDigest())
    with pytest.raises(RuntimeError, match="BASE_IMAGE_DIGEST_MISMATCH"):
        provider.provision("m1", ("atlas", "borealis"))


def test_full_rehearsal_blue_red_teardown(tmp_path: Path) -> None:
    host = ArenaAwareHost()
    provider = LocalDockerRunnerProvider(_config(tmp_path), host=host)
    backend = ProductionRunnerBackend(provider)
    report = backend.rehearse(match_id="m1", runner_names=("atlas", "borealis"))
    assert report.terminal_code == "RUNNERS_DESTROYED"
    assert all(item.state is TeardownState.DESTROYED for item in report.teardown_evidence)
    assert all(check.passed for check in report.preflight_checks)


def test_red_joins_internal_arena_network(tmp_path: Path) -> None:
    host = ArenaAwareHost()
    provider = LocalDockerRunnerProvider(_config(tmp_path), host=host)
    runners = provider.provision("m1", ("atlas", "borealis"))
    observation = provider.network_observation(Phase.RED, runners)
    assert observation.edges == frozenset({
        "atlas:private-a", "borealis:private-b",
        "atlas->toy-service", "borealis->toy-service",
    })
    assert not observation.direct_egress
    connects = [argv for argv in host.argv if argv[:2] == ("network", "connect")]
    assert len(connects) == 2
    disconnects = [argv for argv in host.argv if argv[:2] == ("network", "disconnect")]
    assert len(disconnects) == 2
    assert all(argv[2] == "none" for argv in disconnects)
    # Disconnect precedes connect: a `none`-networked container cannot join
    # a second network directly.
    assert host.argv.index(disconnects[0]) < host.argv.index(connects[0])
    created = [argv for argv in host.argv if argv[:2] == ("network", "create")]
    assert created and "--internal" in " ".join(created[0])
    for runner in runners:
        provider.destroy(runner)


def test_duplicate_match_rejected(tmp_path: Path) -> None:
    host = ArenaAwareHost()
    provider = LocalDockerRunnerProvider(_config(tmp_path), host=host)
    runners = provider.provision("m1", ("atlas", "borealis"))
    with pytest.raises(RuntimeError, match="MATCH_ARTIFACTS_ALREADY_EXIST"):
        provider.provision("m1", ("atlas", "borealis"))
    for runner in runners:
        provider.destroy(runner)


def test_ttl_expiry_fails_closed(tmp_path: Path) -> None:
    host = ArenaAwareHost()
    provider = LocalDockerRunnerProvider(_config(tmp_path, ttl_seconds=30), host=host)
    runners = provider.provision("m1", ("atlas", "borealis"))
    for record in provider._records.values():
        record.deadline = time.monotonic() - 1
        if record.timer is not None:
            record.timer.cancel()
    with pytest.raises(RuntimeError, match="TTL_EXPIRED"):
        provider.probe(runners)


def test_destroy_survivor_quarantines(tmp_path: Path) -> None:
    host = ArenaAwareHost()
    provider = LocalDockerRunnerProvider(_config(tmp_path), host=host)
    runners = provider.provision("m1", ("atlas", "borealis"))
    container = provider._records[runners[0].runner_id].container
    host.destroy_fail.add(container)
    evidence = provider.destroy(runners[0])
    assert evidence.state is TeardownState.QUARANTINED
    provider.destroy(runners[1])
