from __future__ import annotations

import subprocess
import sys
import json
from pathlib import Path


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
