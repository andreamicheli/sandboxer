from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_socket_witness_creates_and_unlinks_only_its_private_probe(tmp_path: Path) -> None:
    directory = tmp_path / "runner"
    directory.mkdir(mode=0o700)
    control_socket = directory / "control.sock"

    result = subprocess.run(
        [sys.executable, "-m", "sandboxer_v0.local_kvm_socket_witness", str(directory), str(control_socket)],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == "SOCKET_WITNESS_OK\n"
    assert not control_socket.exists()
    assert list(directory.iterdir()) == []
