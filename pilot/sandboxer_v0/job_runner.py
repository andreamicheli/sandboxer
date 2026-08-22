"""Persistent supervised job runner for long-running Sandboxer pipelines.

Pipeline stages (match orchestration, TTS synthesis, render) must survive
the tool session that started them: a previous run lost an orchestrator to a
session teardown (leaving orphan QEMU processes and partial telemetry), then
rejected a relaunch of the same Match ID even though the first launch had
actually started.  This module makes detached jobs observable and
controllable:

- ``launch(command_args, job_id, log_path)`` starts the command fully
  detached via ``start_new_session=True`` (``setsid``), records its PID in
  ``pilot/logs/jobs/<job_id>.pid``, appends stdout/stderr to ``log_path``
  with line buffering, and returns immediately.
- ``status(job_id)`` reports ``running``, ``exited`` (with the exit code
  when available) or ``unknown`` from the PID file plus an exit record that
  the supervisor writes when the job ends.
- ``stop(job_id)`` SIGTERMs the whole process group and escalates to
  SIGKILL after ``timeout`` seconds; every job is its own session leader,
  so ``killpg`` reaches wrapper shells and their children.
- Launching an already-running job id raises ``JobAlreadyRunningError``;
  callers retry with ``new_job_id(base)``, which mints ``base-<shortuuid>``.

Each job runs under a tiny supervisor process (this module re-invoked with
the hidden ``_supervise`` subcommand).  The supervisor is the session
leader, keeps the actual command in its process group, reaps it, records
the exit code, and removes the PID file -- so telemetry survives even when
the original launcher is long gone.

CLI for sessions that may die::

    python -m sandboxer_v0.job_runner launch --job-id match-abc \\
        --log pilot/logs/match-abc.log -- python -m my_match_orchestrator ...
    python -m sandboxer_v0.job_runner status match-abc
    python -m sandboxer_v0.job_runner stop match-abc --timeout 10

Exit codes: ``launch`` exits 3 on duplicate running id, 2 on usage errors,
1 on other failures; ``stop`` exits 0 when the job reached ``exited`` and 1
when nothing known was running; ``status`` exits 0 whenever it can report.
The job storage root defaults to ``pilot/logs/jobs`` and can be overridden
per call or with the ``SANDBOXER_JOB_RUNNER_DIR`` environment variable.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

PILOT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_JOBS_DIR = PILOT_ROOT / "logs" / "jobs"
JOBS_DIR_ENV = "SANDBOXER_JOB_RUNNER_DIR"
DEFAULT_STOP_TIMEOUT_S = 5.0
POLL_INTERVAL_S = 0.05
SUPERVISOR_MARKER = "sandboxer_v0.job_runner"
_BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

# Shims spawned by this process; swept opportunistically so long-lived
# callers (dashboards, test suites) do not accumulate zombie supervisors.
_ACTIVE_POPENS: dict[int, subprocess.Popen] = {}


class JobRunnerError(RuntimeError):
    """Base error for job runner failures."""


class JobAlreadyRunningError(JobRunnerError):
    """Raised when launching a job id whose process group is still alive."""

    def __init__(self, job_id: str, pid: int | None = None) -> None:
        self.job_id = job_id
        self.pid = pid
        super().__init__(f"job {job_id!r} is already running (pid={pid}); use new_job_id() for a fresh id")


@dataclass(frozen=True)
class JobStatus:
    job_id: str
    state: str  # running|exited|unknown
    pid: int | None = None
    returncode: int | None = None


@dataclass(frozen=True)
class StopResult:
    job_id: str
    state: str
    pid: int | None = None
    returncode: int | None = None
    stopped_by: str | None = None  # sigterm|sigkill


def resolve_jobs_dir(jobs_dir: Path | str | None = None) -> Path:
    root = Path(jobs_dir or os.environ.get(JOBS_DIR_ENV) or DEFAULT_JOBS_DIR)
    root.mkdir(parents=True, exist_ok=True)
    return root


def new_job_id(base: str) -> str:
    stem = base.strip().rstrip("-")
    if not stem:
        raise ValueError("new_job_id() requires a non-empty base")
    chars = []
    value = uuid.uuid4().int
    for _ in range(12):
        value, rem = divmod(value, 58)
        chars.append(_BASE58[rem])
    return f"{stem}-{''.join(chars)}"


def _validate_job_id(job_id: str) -> str:
    if not job_id or job_id in {".", ".."} or any(ch in job_id for ch in "/\\\0 "):
        raise ValueError(f"invalid job id: {job_id!r}")
    return job_id


def _atomic_write_json(path: Path, payload: dict) -> None:
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, path)


def _read_exit_record(path: Path) -> int | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))["returncode"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        return stat[stat.rindex(")") + 2] != "Z"
    except (OSError, ValueError):
        return True


def _is_job_supervisor(pid: int) -> bool:
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "replace")
    except OSError:
        return False
    spaced = cmdline.replace("\0", " ")
    return SUPERVISOR_MARKER in spaced and "--exit-file" in spaced


def _finish_job(pid_path: Path, exit_path: Path, returncode: int | None) -> None:
    _atomic_write_json(exit_path, {"returncode": returncode})
    pid_path.unlink(missing_ok=True)


def _group_alive(pgid: int) -> bool:
    """True when the group has at least one live member; zombies do not count."""
    if process_group_members(pgid):
        return True
    if Path("/proc").is_dir():
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def process_group_members(pgid: int) -> list[int]:
    """Live (non-zombie) pids whose process group is ``pgid``."""
    members = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="utf-8")
            fields = stat[stat.rindex(")") + 2:].split()
            if int(fields[2]) == pgid and fields[0] != "Z":
                members.append(int(entry.name))
        except (OSError, ValueError, IndexError):
            continue
    return members


def _reap_finished_shims() -> None:
    for pid, proc in list(_ACTIVE_POPENS.items()):
        if proc.poll() is not None:
            del _ACTIVE_POPENS[pid]


def launch(
    command_args: list[str],
    job_id: str,
    log_path: Path | str,
    *,
    jobs_dir: Path | str | None = None,
) -> JobStatus:
    """Start ``command_args`` detached under supervisor ``job_id`` and return immediately."""
    _validate_job_id(job_id)
    if not command_args:
        raise JobRunnerError("launch requires a non-empty command")
    _reap_finished_shims()
    root = resolve_jobs_dir(jobs_dir)
    pid_path = root / f"{job_id}.pid"
    exit_path = root / f"{job_id}.exit.json"
    current = status(job_id, jobs_dir=root)
    if current.state == "running":
        raise JobAlreadyRunningError(job_id, current.pid)
    pid_path.unlink(missing_ok=True)
    exit_path.unlink(missing_ok=True)

    log_file = Path(log_path)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    package_parent = str(PILOT_ROOT)
    env["PYTHONPATH"] = (
        f"{package_parent}{os.pathsep}{env['PYTHONPATH']}" if env.get("PYTHONPATH") else package_parent
    )
    handle = open(log_file, "a", encoding="utf-8", buffering=1)
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                SUPERVISOR_MARKER,
                "_supervise",
                "--pid-file",
                str(pid_path),
                "--exit-file",
                str(exit_path),
                "--",
                *command_args,
            ],
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
            env=env,
        )
    finally:
        handle.close()
    _ACTIVE_POPENS[proc.pid] = proc
    pid_path.write_text(f"{proc.pid}\n", encoding="ascii")
    return JobStatus(job_id=job_id, state="running", pid=proc.pid, returncode=None)


def status(job_id: str, *, jobs_dir: Path | str | None = None) -> JobStatus:
    """Report running|exited|unknown for ``job_id``, with exit code if available."""
    _validate_job_id(job_id)
    _reap_finished_shims()
    root = resolve_jobs_dir(jobs_dir)
    pid_path = root / f"{job_id}.pid"
    exit_path = root / f"{job_id}.exit.json"
    pid = _read_pid(pid_path)
    supervisor_alive = pid is not None and _process_alive(pid) and _is_job_supervisor(pid)
    if exit_path.exists():
        return JobStatus(job_id=job_id, state="exited", pid=None if not pid_path.exists() else pid, returncode=_read_exit_record(exit_path))
    if supervisor_alive:
        return JobStatus(job_id=job_id, state="running", pid=pid, returncode=None)
    return JobStatus(job_id=job_id, state="unknown", pid=pid, returncode=None)


def stop(job_id: str, *, timeout: float = DEFAULT_STOP_TIMEOUT_S, jobs_dir: Path | str | None = None) -> StopResult:
    """SIGTERM the job's process group, escalating to SIGKILL after ``timeout``."""
    _validate_job_id(job_id)
    _reap_finished_shims()
    root = resolve_jobs_dir(jobs_dir)
    pid_path = root / f"{job_id}.pid"
    exit_path = root / f"{job_id}.exit.json"
    current = status(job_id, jobs_dir=root)
    if current.state == "exited":
        return StopResult(job_id=job_id, state="exited", stopped_by=None)
    pid = current.pid
    if pid is None or not _is_job_supervisor(pid) or not _process_alive(pid):
        return StopResult(job_id=job_id, state=current.state, pid=pid, stopped_by=None)

    stopped_by = "sigterm"
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + timeout
    while _group_alive(pid) and time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL_S)
    if _group_alive(pid):
        stopped_by = "sigkill"
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + timeout
        while _group_alive(pid) and time.monotonic() < deadline:
            time.sleep(POLL_INTERVAL_S)
    _finish_job(pid_path, exit_path, None)
    return StopResult(job_id=job_id, state="exited", pid=pid, returncode=None, stopped_by=stopped_by)


def _supervise(pid_path: Path, exit_path: Path, command: list[str]) -> int:
    try:
        proc = subprocess.Popen(command, stdin=subprocess.DEVNULL)
    except OSError as exc:
        print(f"job_runner: failed to start {command}: {exc}", file=sys.stderr, flush=True)
        _finish_job(pid_path, exit_path, 127)
        return 127
    returncode = proc.wait()
    _finish_job(pid_path, exit_path, returncode)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="sandboxer_v0.job_runner", description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)

    def add_jobs_dir(target: argparse.ArgumentParser) -> None:
        target.add_argument("--jobs-dir", type=Path, default=None, help=f"job storage root (default: {DEFAULT_JOBS_DIR})")

    p_launch = sub.add_parser("launch", help="start a detached supervised job; command follows '--'")
    p_launch.add_argument("--job-id", required=True)
    p_launch.add_argument("--log", type=Path, required=True)
    add_jobs_dir(p_launch)

    p_status = sub.add_parser("status", help="report running|exited|unknown for a job id")
    p_status.add_argument("job_id")
    add_jobs_dir(p_status)

    p_stop = sub.add_parser("stop", help="SIGTERM then SIGKILL the job's process group")
    p_stop.add_argument("job_id")
    p_stop.add_argument("--timeout", type=float, default=DEFAULT_STOP_TIMEOUT_S)
    add_jobs_dir(p_stop)

    p_supervise = sub.add_parser("_supervise", help=argparse.SUPPRESS)
    p_supervise.add_argument("--pid-file", type=Path, required=True)
    p_supervise.add_argument("--exit-file", type=Path, required=True)
    p_supervise.add_argument("command", nargs=argparse.REMAINDER)

    command: list[str] = []
    if argv and argv[0] == "launch" and "--" in argv:
        split = argv.index("--")
        head, command = argv[:split], argv[split + 1:]
    else:
        head = argv
    args = parser.parse_args(head)

    if args.action == "_supervise":
        if args.command and args.command[0] == "--":
            args.command = args.command[1:]
        return _supervise(args.pid_file, args.exit_file, args.command)

    if args.action == "launch":
        if not command:
            parser.error("launch requires a command after '--'")
        try:
            result: JobStatus | StopResult = launch(command, args.job_id, args.log, jobs_dir=args.jobs_dir)
        except JobAlreadyRunningError as exc:
            print(json.dumps({"error": str(exc), "job_id": exc.job_id, "pid": exc.pid}, sort_keys=True), file=sys.stderr)
            return 3
    elif args.action == "status":
        result = status(args.job_id, jobs_dir=args.jobs_dir)
    elif args.action == "stop":
        result = stop(args.job_id, timeout=args.timeout, jobs_dir=args.jobs_dir)
    else:
        parser.error(f"unknown action {args.action!r}")
    print(json.dumps(asdict(result), sort_keys=True, separators=(",", ":")))
    return 0 if (args.action != "stop" or result.state == "exited") else 1


if __name__ == "__main__":
    raise SystemExit(main())
