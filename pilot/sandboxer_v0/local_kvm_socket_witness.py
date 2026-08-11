"""Prove that the eventual non-root QEMU identity can use a Runner directory."""

from __future__ import annotations

import argparse
import os
import secrets
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("directory", type=Path)
    parser.add_argument("control_socket", type=Path)
    arguments = parser.parse_args(argv)
    directory = arguments.directory.resolve()
    control_socket = arguments.control_socket.absolute()
    if control_socket.parent != directory or control_socket.name != "control.sock":
        return 2
    try:
        os.lstat(control_socket)
    except FileNotFoundError:
        pass
    else:
        return 3
    probe = directory / f".sandboxer-socket-witness-{secrets.token_hex(8)}"
    descriptor = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    os.unlink(probe)
    print("SOCKET_WITNESS_OK")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through its CLI seam
    raise SystemExit(main())
