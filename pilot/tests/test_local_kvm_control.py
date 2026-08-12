from __future__ import annotations

import pytest

from sandboxer_v0.local_kvm_control import ControlProbe, ControlReady, parse_control, parse_network_proof


NONCE = "a" * 64
BOOT_ID = "11111111-1111-1111-1111-111111111111"


def test_typed_control_probe_requires_exact_measured_fields() -> None:
    response = (
        f"READY nonce={NONCE} uid=1001 boot_id={BOOT_ID} no_credentials=1 private_mounts=1 route_after_setup=absent route_at_control=absent\n"
        f"PROBE_OK nonce={NONCE} uid=1001 clock_epoch=1720000000\n"
    )

    probe = parse_control(response, NONCE, require_probe=True)

    assert isinstance(probe, ControlProbe)
    assert probe.uid == 1001
    assert probe.private_mounts is True


@pytest.mark.parametrize("invalid", ["yes", "true", "2"])
def test_control_rejects_non_boolean_or_conflicting_claims(invalid: str) -> None:
    response = f"READY nonce={NONCE} uid=1001 boot_id={BOOT_ID} no_credentials={invalid} private_mounts=1 route_after_setup=absent route_at_control=absent\n"

    with pytest.raises(RuntimeError, match="CONTROL_PROBE_INVALID"):
        parse_control(response, NONCE, require_probe=False)

    conflicting = (
        f"READY nonce={NONCE} uid=1001 boot_id={BOOT_ID} no_credentials=1 private_mounts=1 route_after_setup=absent route_at_control=absent\n"
        f"PROBE_OK nonce={NONCE} uid=0 clock_epoch=1720000000\n"
    )
    with pytest.raises(RuntimeError, match="CONTROL_PROBE_INVALID"):
        parse_control(conflicting, NONCE, require_probe=True)


def test_ready_without_probe_is_a_distinct_typed_evidence_shape() -> None:
    ready = parse_control(
        f"READY nonce={NONCE} uid=1001 boot_id={BOOT_ID} no_credentials=1 private_mounts=1 route_after_setup=absent route_at_control=absent\n",
        NONCE,
        require_probe=False,
    )

    assert isinstance(ready, ControlReady)
    assert not isinstance(ready, ControlProbe)


def test_control_accepts_unknown_route_marker_but_rejects_invalid_value() -> None:
    unknown = parse_control(
        f"READY nonce={NONCE} uid=1001 boot_id={BOOT_ID} no_credentials=1 private_mounts=1 route_after_setup=absent route_at_control=unknown\n",
        NONCE, require_probe=False,
    )
    assert unknown.route_at_control == "unknown"
    with pytest.raises(RuntimeError, match="CONTROL_PROBE_INVALID"):
        parse_control(
            f"READY nonce={NONCE} uid=1001 boot_id={BOOT_ID} no_credentials=1 private_mounts=1 route_after_setup=absent route_at_control=maybe\n",
            NONCE, require_probe=False,
        )


def test_control_route_origin_diagnostics_are_categorical_and_strict() -> None:
    ready = parse_control(
        f"READY nonce={NONCE} uid=1001 boot_id={BOOT_ID} no_credentials=1 private_mounts=1 route_after_setup=absent route_at_control=present route_origin=dhcp dhcp_client=1\n",
        NONCE, require_probe=False,
    )
    assert ready.route_origin == "dhcp" and ready.dhcp_client == "1"
    with pytest.raises(RuntimeError, match="CONTROL_PROBE_INVALID"):
        parse_control(
            f"READY nonce={NONCE} uid=1001 boot_id={BOOT_ID} no_credentials=1 private_mounts=1 route_after_setup=absent route_at_control=present route_origin=10.77.0.1 dhcp_client=maybe\n",
            NONCE, require_probe=False,
        )


@pytest.mark.parametrize("toy_bootstrap", [
    "ready", "root_failed", "exec_failed", "bind_failed", "exited_other", "unknown",
    # Retain parser compatibility with the previous audited image evidence.
    "root_missing", "launch_failed", "listener_missing",
])
def test_control_accepts_each_declared_toy_bootstrap_state(toy_bootstrap: str) -> None:
    """The immutable guest's current READY schema is accepted end-to-end."""
    response = (
        f"READY nonce={NONCE} uid=1001 boot_id={BOOT_ID} no_credentials=1 private_mounts=1 "
        f"route_after_setup=absent route_at_control=absent route_origin=absent dhcp_client=0 "
        f"toy_bootstrap={toy_bootstrap}\n"
        f"PROBE_OK nonce={NONCE} uid=1001 clock_epoch=1720000000\n"
    )

    probe = parse_control(response, NONCE, require_probe=True)

    assert probe.toy_bootstrap == toy_bootstrap


def test_control_rejects_incoherent_route_snapshot() -> None:
    """A route state and origin must represent the same control-start read."""
    response = (
        f"READY nonce={NONCE} uid=1001 boot_id={BOOT_ID} no_credentials=1 private_mounts=1 "
        "route_after_setup=absent route_at_control=absent route_origin=other dhcp_client=1 toy_bootstrap=ready\n"
        f"PROBE_OK nonce={NONCE} uid=1001 clock_epoch=1720000000\n"
    )

    with pytest.raises(RuntimeError, match="CONTROL_PROBE_INVALID"):
        parse_control(response, NONCE, require_probe=True)


def test_control_rejects_fields_rendered_on_the_wrong_protocol_line() -> None:
    """READY and PROBE_OK have distinct strict schemas, regardless of order."""
    response = (
        f"READY nonce={NONCE}\n"
        f"PROBE_OK nonce={NONCE} uid=1001 boot_id={BOOT_ID} no_credentials=1 private_mounts=1 "
        "route_after_setup=absent route_at_control=absent clock_epoch=1720000000\n"
    )

    with pytest.raises(RuntimeError, match="CONTROL_PROBE_INVALID"):
        parse_control(response, NONCE, require_probe=True)


def test_network_proof_requires_all_active_denial_checks() -> None:
    proof = parse_network_proof(
        f"NETWORK_PROBE nonce={NONCE} phase=red peer_denied=0 toy_http=1 alternate_denied=1 icmp_denied=1 egress_denied=1 egress_reason=blocked orchestrator_denied=1\n",
        NONCE, "red",
    )
    assert proof.toy_http is True
    assert proof.egress_reason == "blocked"
    with pytest.raises(RuntimeError, match="NETWORK_PROOF_INVALID"):
        parse_network_proof(
            f"NETWORK_PROBE nonce={NONCE} phase=red peer_denied=0 toy_http=1 alternate_denied=1 icmp_denied=1 egress_denied=1 egress_reason=default_route orchestrator_denied=1\n",
            NONCE, "red",
        )
    with pytest.raises(RuntimeError, match="NETWORK_PROOF_INVALID"):
        parse_network_proof(f"NETWORK_PROBE nonce={NONCE} phase=red toy_http=1\n", NONCE, "red")
