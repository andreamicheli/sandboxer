from __future__ import annotations

import logging

import pytest

from sandboxer_v0.arena_safety import Phase
from sandboxer_v0.local_kvm_control import NetworkProof
from sandboxer_v0.runner_backend import PreflightWitnessFailed

from test_local_kvm_provider import configured_provider


def _valid_proof(payload: str) -> str:
    _, nonce, phase = payload.split()
    if phase == "blue":
        return (
            f"NETWORK_PROBE nonce={nonce} phase=blue peer_denied=1 toy_http=0 alternate_denied=1 "
            "icmp_denied=1 egress_denied=1 egress_reason=blocked orchestrator_denied=1\n"
        )
    return (
        f"NETWORK_PROBE nonce={nonce} phase={phase} peer_denied=0 toy_http=1 alternate_denied=1 "
        "icmp_denied=1 egress_denied=1 egress_reason=blocked orchestrator_denied=1\n"
    )


def _scripted_exchange(responses):
    attempts = {"count": 0}

    def exchange(socket_path, payload, *, timeout_seconds: float = 5) -> str:
        del socket_path, timeout_seconds
        index = attempts["count"]
        attempts["count"] += 1
        outcome = responses[index] if index < len(responses) else responses[-1]
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            return outcome(payload)
        return outcome

    return exchange, attempts


def _provisioned_record(provider, runners):
    return provider._records[runners[0].runner_id]


def test_netprobe_recovers_when_the_guest_returns_garbage_once_during_boot(tmp_path, monkeypatch) -> None:
    provider, host = configured_provider(tmp_path)
    runners = provider.provision("netprobe-garbage-once", ("atlas", "borealis"))
    record = _provisioned_record(provider, runners)
    exchange, attempts = _scripted_exchange(["guest control daemon is still booting\n", _valid_proof])
    monkeypatch.setattr(host, "control_exchange", exchange)
    waits: list[float] = []
    monkeypatch.setattr("sandboxer_v0.local_kvm.time.sleep", waits.append)

    proof = provider._network_proof(record, Phase.BLUE)

    assert isinstance(proof, NetworkProof)
    assert proof.nonce == record.nonce
    assert proof.phase == "blue"
    assert attempts["count"] == 2
    assert waits == [0.5]


def test_netprobe_survives_two_guest_boot_timeouts_before_a_valid_proof(tmp_path, monkeypatch) -> None:
    provider, host = configured_provider(tmp_path)
    runners = provider.provision("netprobe-timeout-twice", ("atlas", "borealis"))
    record = _provisioned_record(provider, runners)
    exchange, attempts = _scripted_exchange([TimeoutError("slow guest boot"), TimeoutError("still slow"), _valid_proof])
    monkeypatch.setattr(host, "control_exchange", exchange)
    waits: list[float] = []
    monkeypatch.setattr("sandboxer_v0.local_kvm.time.sleep", waits.append)

    proof = provider._network_proof(record, Phase.RED)

    assert isinstance(proof, NetworkProof)
    assert proof.nonce == record.nonce
    assert proof.phase == "red"
    assert attempts["count"] == 3
    assert waits == [0.5, 1.0]


def test_netprobe_retries_a_connection_refused_from_a_reopening_guest_port(tmp_path, monkeypatch) -> None:
    provider, host = configured_provider(tmp_path)
    runners = provider.provision("netprobe-refused-once", ("atlas", "borealis"))
    record = _provisioned_record(provider, runners)
    exchange, attempts = _scripted_exchange([ConnectionRefusedError(111, "virtio port reopening"), _valid_proof])
    monkeypatch.setattr(host, "control_exchange", exchange)
    waits: list[float] = []
    monkeypatch.setattr("sandboxer_v0.local_kvm.time.sleep", waits.append)

    proof = provider._network_proof(record, Phase.BLUE)

    assert isinstance(proof, NetworkProof)
    assert attempts["count"] == 2
    assert waits == [0.5]


@pytest.mark.parametrize(("phase", "reason_suffix"), [(Phase.BLUE, "BLUE"), (Phase.RED, "RED")])
def test_netprobe_fails_closed_with_the_original_reason_after_three_invalid_responses(
    tmp_path, monkeypatch, phase: Phase, reason_suffix: str
) -> None:
    provider, host = configured_provider(tmp_path)
    runners = provider.provision(f"netprobe-garbage-{reason_suffix.lower()}", ("atlas", "borealis"))
    record = _provisioned_record(provider, runners)
    exchange, attempts = _scripted_exchange(["not a network proof\n"])
    monkeypatch.setattr(host, "control_exchange", exchange)
    waits: list[float] = []
    monkeypatch.setattr("sandboxer_v0.local_kvm.time.sleep", waits.append)

    with pytest.raises(PreflightWitnessFailed) as error:
        provider._network_proof(record, phase)

    assert error.value.reason_code == f"LOCAL_KVM_{reason_suffix}_GUEST_NETPROBE_INVALID_RESPONSE"
    assert attempts["count"] == 3
    assert waits == [0.5, 1.0]


def test_netprobe_keeps_the_timeout_reason_code_after_exhausting_all_attempts(tmp_path, monkeypatch) -> None:
    provider, host = configured_provider(tmp_path)
    runners = provider.provision("netprobe-timeout-exhausted", ("atlas", "borealis"))
    record = _provisioned_record(provider, runners)
    exchange, attempts = _scripted_exchange([TimeoutError("guest never answered")])
    monkeypatch.setattr(host, "control_exchange", exchange)
    waits: list[float] = []
    monkeypatch.setattr("sandboxer_v0.local_kvm.time.sleep", waits.append)

    with pytest.raises(PreflightWitnessFailed) as error:
        provider._network_proof(record, Phase.BLUE)

    assert error.value.reason_code == "LOCAL_KVM_BLUE_GUEST_NETPROBE_TIMEOUT"
    assert attempts["count"] == 3
    assert waits == [0.5, 1.0]


def test_netprobe_retry_emits_a_debug_telemetry_line_per_failed_attempt(tmp_path, monkeypatch, caplog) -> None:
    provider, host = configured_provider(tmp_path)
    runners = provider.provision("netprobe-retry-telemetry", ("atlas", "borealis"))
    runner_record = _provisioned_record(provider, runners)
    exchange, _attempts = _scripted_exchange([TimeoutError("slow"), "garbage\n", _valid_proof])
    monkeypatch.setattr(host, "control_exchange", exchange)
    monkeypatch.setattr("sandboxer_v0.local_kvm.time.sleep", lambda _seconds: None)

    with caplog.at_level(logging.DEBUG, logger="sandboxer_v0.local_kvm"):
        provider._network_proof(runner_record, Phase.BLUE)

    messages = [item.getMessage() for item in caplog.records]
    assert sum(message.startswith("netprobe_retry attempt=") for message in messages) == 2
    assert any(
        message.startswith("netprobe_retry attempt=2") and "phase=blue" in message
        and runner_record.handle.runner_id in message and "TimeoutError" in message
        for message in messages
    )
    assert any(message.startswith("netprobe_retry attempt=3") and "RuntimeError" in message for message in messages)
