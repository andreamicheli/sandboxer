"""Strict, bounded control-channel evidence for local KVM Runners."""

from __future__ import annotations

import re
from dataclasses import dataclass


_BOOT_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f-]{27}\Z")


@dataclass(frozen=True)
class ControlReady:
    nonce: str
    uid: int
    boot_id: str
    no_credentials: bool
    private_mounts: bool
    route_after_setup: str
    route_at_control: str


@dataclass(frozen=True)
class ControlProbe(ControlReady):
    clock_epoch: int


@dataclass(frozen=True)
class NetworkProof:
    nonce: str
    phase: str
    peer_denied: bool
    toy_http: bool
    alternate_denied: bool
    icmp_denied: bool
    egress_denied: bool
    egress_reason: str
    orchestrator_denied: bool


def parse_control(response: str, nonce: str, *, require_probe: bool) -> ControlReady | ControlProbe:
    fields: dict[str, str] = {}
    known = {"nonce", "uid", "boot_id", "no_credentials", "private_mounts", "route_after_setup", "route_at_control", "clock_epoch"}
    saw_ready = False
    saw_probe = False
    for line in response.splitlines():
        if not line:
            continue
        if line.startswith("READY "):
            saw_ready = True
        elif line.startswith("PROBE_OK "):
            saw_probe = True
        else:
            raise RuntimeError("CONTROL_PROBE_INVALID")
        for token in line.split()[1:]:
            key, separator, value = token.partition("=")
            if not separator or key not in known or (key in fields and fields[key] != value):
                raise RuntimeError("CONTROL_PROBE_INVALID")
            fields[key] = value
    required = {"nonce", "uid", "boot_id", "no_credentials", "private_mounts", "route_after_setup", "route_at_control"}
    if require_probe:
        required.add("clock_epoch")
    if not saw_ready or (require_probe and not saw_probe) or required - fields.keys():
        raise RuntimeError("CONTROL_PROBE_INVALID")
    if fields["nonce"] != nonce or not _BOOT_ID.fullmatch(fields["boot_id"]):
        raise RuntimeError("CONTROL_PROBE_INVALID")
    if fields["no_credentials"] not in {"0", "1"} or fields["private_mounts"] not in {"0", "1"}:
        raise RuntimeError("CONTROL_PROBE_INVALID")
    if fields["route_after_setup"] not in {"absent", "present"} or fields["route_at_control"] not in {"absent", "present"}:
        raise RuntimeError("CONTROL_PROBE_INVALID")
    try:
        ready = ControlReady(
            nonce=fields["nonce"], uid=int(fields["uid"]), boot_id=fields["boot_id"],
            no_credentials=fields["no_credentials"] == "1", private_mounts=fields["private_mounts"] == "1",
            route_after_setup=fields["route_after_setup"], route_at_control=fields["route_at_control"],
        )
        if require_probe:
            return ControlProbe(**ready.__dict__, clock_epoch=int(fields["clock_epoch"]))
    except (TypeError, ValueError) as error:
        raise RuntimeError("CONTROL_PROBE_INVALID") from error
    return ready


def parse_network_proof(response: str, nonce: str, phase: str) -> NetworkProof:
    expected = {"nonce", "phase", "peer_denied", "toy_http", "alternate_denied", "icmp_denied", "egress_denied", "egress_reason", "orchestrator_denied"}
    lines = response.splitlines()
    if len(lines) != 1 or not lines[0].startswith("NETWORK_PROBE "):
        raise RuntimeError("NETWORK_PROOF_INVALID")
    fields: dict[str, str] = {}
    for token in lines[0].split()[1:]:
        key, separator, value = token.partition("=")
        if not separator or key not in expected or key in fields:
            raise RuntimeError("NETWORK_PROOF_INVALID")
        fields[key] = value
    if fields.keys() != expected or fields["nonce"] != nonce or fields["phase"] != phase:
        raise RuntimeError("NETWORK_PROOF_INVALID")
    boolean_fields = expected - {"nonce", "phase", "egress_reason"}
    if any(fields[key] not in {"0", "1"} for key in boolean_fields):
        raise RuntimeError("NETWORK_PROOF_INVALID")
    if fields["egress_reason"] not in {"blocked", "default_route", "tcp_reachable"}:
        raise RuntimeError("NETWORK_PROOF_INVALID")
    if (fields["egress_denied"] == "1") != (fields["egress_reason"] == "blocked"):
        raise RuntimeError("NETWORK_PROOF_INVALID")
    return NetworkProof(
        nonce=nonce, phase=phase,
        peer_denied=fields["peer_denied"] == "1", toy_http=fields["toy_http"] == "1",
        alternate_denied=fields["alternate_denied"] == "1", icmp_denied=fields["icmp_denied"] == "1",
        egress_denied=fields["egress_denied"] == "1", egress_reason=fields["egress_reason"],
        orchestrator_denied=fields["orchestrator_denied"] == "1",
    )
