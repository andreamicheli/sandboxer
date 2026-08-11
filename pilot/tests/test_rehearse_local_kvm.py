from __future__ import annotations

import subprocess
import sys
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
