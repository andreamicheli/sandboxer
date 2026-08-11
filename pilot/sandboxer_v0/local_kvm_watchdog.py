"""External TTL helper; invoked by systemd-run with argv only."""
from __future__ import annotations

import sys
from pathlib import Path


def expire(pid: int, starttime: str, cgroup: Path) -> int:
    if not cgroup.is_absolute() or not str(cgroup).startswith("/sys/fs/cgroup/sandboxer/"):
        return 2
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
    except FileNotFoundError:
        return 0
    if len(fields) <= 21 or fields[21] != starttime:
        return 0
    Path(cgroup, "cgroup.kill").write_text("1", encoding="ascii")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        return 2
    try:
        return expire(int(argv[0]), argv[1], Path(argv[2]))
    except (OSError, ValueError):
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
