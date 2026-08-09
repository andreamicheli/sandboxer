from pathlib import Path


def test_pristine_service_is_toy_only() -> None:
    source = Path("arena/pristine/service.py").read_text(encoding="utf-8")
    assert "ThreadingHTTPServer" in source
    assert "subprocess" not in source
    assert "socket." not in source
    assert "/api/export" in source


def test_reference_hardening_contains_resolved_path() -> None:
    source = Path("arena/hardened/service.py").read_text(encoding="utf-8")
    assert "DATA_ROOT not in candidate.parents" in source
    assert "outside_data_root" in source
    assert "subprocess" not in source


def test_no_host_paths_in_compose_source() -> None:
    source = Path("compose.yaml").read_text(encoding="utf-8")
    assert "/Users/" not in source
    assert "docker.sock" not in source
    assert "network_mode: host" not in source
