from scripts.model_gate import model_connections_disabled
from scripts.preflight import validate_colima, validate_compose


def _model() -> dict[str, object]:
    runner = {
        "privileged": False,
        "user": "10001:10001",
        "read_only": True,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "pids_limit": 10,
        "mem_limit": "1g",
        "cpus": 1,
        "networks": {"alpha_private": None},
        "volumes": [{"type": "volume", "source": "work", "target": "/arena"}],
    }
    return {
        "services": {"alpha": dict(runner), "beta": {**runner, "networks": {"beta_private": None}}},
        "networks": {"alpha_private": {"internal": True}, "beta_private": {"internal": True}},
    }


def test_valid_model_passes() -> None:
    assert validate_compose(_model()) == []


def test_bind_mount_fails_closed() -> None:
    model = _model()
    model["services"]["alpha"]["volumes"] = [
        {"type": "bind", "source": "/Users/example", "target": "/arena"}
    ]
    assert any("bind mount" in failure for failure in validate_compose(model))


def test_direct_egress_fails_closed() -> None:
    model = _model()
    model["services"]["beta"]["networks"]["egress"] = None
    assert "beta: direct egress network" in validate_compose(model)


def test_root_runner_fails_closed() -> None:
    model = _model()
    model["services"]["alpha"]["user"] = "0:0"
    assert "alpha: explicit non-root user missing" in validate_compose(model)


def test_shared_blue_network_fails_closed() -> None:
    model = _model()
    model["services"]["beta"]["networks"] = {"alpha_private": None}
    assert any("runners share networks" in failure for failure in validate_compose(model))


def test_repository_records_explicit_live_provider_gate() -> None:
    safe, _ = model_connections_disabled()
    assert not safe


def test_colima_boundary_requires_no_mounts_or_forwarding() -> None:
    assert validate_colima({"mounts": None, "portForwarder": "none", "vmType": "vz", "runtime": "docker"}) == []
    failures = validate_colima({"mounts": [{"location": "/Users"}], "portForwarder": "ssh"})
    assert "host mounts are enabled" in failures
    assert "port forwarding is enabled" in failures
