from __future__ import annotations

import subprocess
import sys
import json
from pathlib import Path

import pytest

from sandboxer_v0.arena_safety import TeardownEvidence, TeardownState
from sandboxer_v0.runner_backend import RunnerPreflightFailed


REHEARSAL = Path(__file__).parents[1] / "scripts" / "rehearse_local_kvm.py"


def test_rehearsal_cli_runs_from_an_unrelated_working_directory(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(REHEARSAL), "--help"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "--match-id" in result.stdout


def test_rehearsal_evidence_writer_is_atomic_private_and_typed(tmp_path: Path) -> None:
    sys.path.insert(0, str(REHEARSAL.parents[1]))
    from scripts.rehearse_local_kvm import _write_evidence

    target = tmp_path / "evidence" / "report.json"
    _write_evidence(target, {"result": "passed", "terminal_code": "RUNNERS_DESTROYED", "production_ready": False, "active_witness_summary": "blue-active-witnesses-passed", "teardown": []})

    assert target.stat().st_mode & 0o777 == 0o600
    assert json.loads(target.read_text())["terminal_code"] == "RUNNERS_DESTROYED"
    assert not list(target.parent.glob(".rehearsal-*"))


def test_rehearsal_cli_persists_a_redacted_typed_failure_before_stderr(tmp_path: Path, monkeypatch) -> None:
    sys.path.insert(0, str(REHEARSAL.parents[1]))
    from scripts import rehearse_local_kvm as cli
    image = tmp_path / "image.qcow2"; image.write_bytes(b"image")
    profile = tmp_path / "profile.json"; profile.write_text("{}")
    evidence = tmp_path / "report.json"

    class Backend:
        def __init__(self, _provider): pass
        def rehearse(self, **_kwargs):
            raise RunnerPreflightFailed("LOCAL_KVM_BLUE_PEER_ISOLATION_WITNESS_FAILED", (
                TeardownEvidence("atlas", TeardownState.DESTROYED, "verified", None),
                TeardownEvidence("borealis", TeardownState.QUARANTINED, "verified", "NAMESPACE_REMAINS"),
            ))

    monkeypatch.setattr(cli, "ProductionRunnerBackend", Backend)
    monkeypatch.setattr(cli, "LocalKvmRunnerProvider", lambda config: config)
    monkeypatch.setattr(cli, "LocalKvmConfig", lambda **kwargs: kwargs)
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cli.pwd, "getpwnam", lambda _name: type("Account", (), {"pw_uid": 1, "pw_gid": 1})())
    assert cli.main(["--image", str(image), "--profile", str(profile), "--match-id", "safe-match", "--evidence-path", str(evidence)]) == 2
    payload = json.loads(evidence.read_text())
    assert payload["reason_code"] == "LOCAL_KVM_BLUE_PEER_ISOLATION_WITNESS_FAILED"
    assert payload["teardown"][1]["state"] == "quarantined"
    assert evidence.stat().st_mode & 0o777 == 0o600
    assert str(tmp_path) not in evidence.read_text() and "image" not in evidence.read_text()


def test_rehearsal_cli_keeps_complete_evidence_if_stdout_fails(tmp_path: Path, monkeypatch) -> None:
    sys.path.insert(0, str(REHEARSAL.parents[1]))
    from scripts import rehearse_local_kvm as cli
    image = tmp_path / "image.qcow2"; image.write_bytes(b"image")
    profile = tmp_path / "profile.json"; profile.write_text("{}")
    evidence = tmp_path / "report.json"

    class Backend:
        def __init__(self, _provider): pass
        def rehearse(self, **_kwargs):
            from sandboxer_v0.runner_backend import RehearsalReport
            return RehearsalReport("RUNNERS_DESTROYED", False, False, ("atlas", "borealis"), (), ())

    monkeypatch.setattr(cli, "ProductionRunnerBackend", Backend)
    monkeypatch.setattr(cli, "LocalKvmRunnerProvider", lambda config: config)
    monkeypatch.setattr(cli, "LocalKvmConfig", lambda **kwargs: kwargs)
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cli.pwd, "getpwnam", lambda _name: type("Account", (), {"pw_uid": 1, "pw_gid": 1})())
    monkeypatch.setattr("builtins.print", lambda *_args, **_kwargs: (_ for _ in ()).throw(BrokenPipeError()))
    with pytest.raises(BrokenPipeError):
        cli.main(["--image", str(image), "--profile", str(profile), "--match-id", "safe-match", "--evidence-path", str(evidence)])
    assert json.loads(evidence.read_text())["result"] == "passed"
    assert not list(evidence.parent.glob(".rehearsal-*"))
