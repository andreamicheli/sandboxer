"""Tests for the persistent supervised job runner."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

from sandboxer_v0 import job_runner as jr

PILOT_ROOT = Path(__file__).resolve().parent.parent
SLEEPER = (
    "import sys, time\n"
    "print('job-up', flush=True)\n"
    "sys.stdout.flush()\n"
    "time.sleep(float(sys.argv[1]))\n"
)


def run_cli(*args: str, jobs_dir: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, SANDBOXER_JOB_RUNNER_DIR=str(jobs_dir))
    return subprocess.run(
        [sys.executable, "-m", "sandboxer_v0.job_runner", *args],
        cwd=PILOT_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )


def wait_until(predicate, timeout: float = 15.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(0.05)
    raise AssertionError(f"condition not met within {timeout}s (last={last!r})")


@pytest.fixture
def sleeper(tmp_path):
    def _sleeper(job_id: str, seconds: float = 30.0, jobs_dir: Path | None = None) -> jr.JobStatus:
        return jr.launch(
            [sys.executable, "-c", SLEEPER, str(seconds)],
            job_id,
            tmp_path / f"{job_id}.log",
            jobs_dir=jobs_dir or (tmp_path / "jobs"),
        )

    yield _sleeper


def stop_quietly(job_id: str, jobs_dir: Path) -> None:
    try:
        jr.stop(job_id, timeout=5.0, jobs_dir=jobs_dir)
    except Exception:
        pass


def test_launch_survives_launcher_death_and_reports_status(tmp_path):
    jobs = tmp_path / "jobs"
    log = tmp_path / "logs" / "detach.log"
    command = shlex.join([sys.executable, "-c", SLEEPER, "30"])
    done = subprocess.run(
        [
            "bash",
            "-c",
            f"{shlex.quote(sys.executable)} -m sandboxer_v0.job_runner launch "
            f"--job-id detach-1 --log {shlex.quote(str(log))} -- {command}; "
            "echo launcher-exited=$?",
        ],
        cwd=PILOT_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        env=dict(os.environ, SANDBOXER_JOB_RUNNER_DIR=str(jobs)),
    )
    assert done.returncode == 0, done.stderr
    assert "launcher-exited=0" in done.stdout
    payload = json.loads(done.stdout.strip().splitlines()[0])
    pid = int((jobs / "detach-1.pid").read_text(encoding="ascii"))
    assert payload["pid"] == pid

    # The launcher process tree is gone; the detached session must still be alive.
    wait_until(lambda: log.exists() and "job-up" in log.read_text(encoding="utf-8"))
    observed = json.loads(run_cli("status", "detach-1", jobs_dir=jobs).stdout)
    assert observed["state"] == "running"
    assert observed["pid"] == pid
    try:
        stopped = json.loads(run_cli("stop", "detach-1", "--timeout", "5", jobs_dir=jobs).stdout)
        assert stopped["state"] == "exited"
        wait_until(lambda not_used=None: not jr._process_alive(pid))
    finally:
        stop_quietly("detach-1", jobs)
    assert not (jobs / "detach-1.pid").exists()


def test_pid_file_lifecycle_records_exit_code_and_log(tmp_path):
    jobs = tmp_path / "jobs"
    log = tmp_path / "logs" / "cycle.log"
    status = jr.launch(
        [sys.executable, "-c", "print('line-one', flush=True); import time; time.sleep(0.6); print('line-done', flush=True)"],
        "cycle-1",
        log,
        jobs_dir=jobs,
    )
    try:
        assert status.state == "running"
        assert status.pid == int((jobs / "cycle-1.pid").read_text(encoding="ascii"))
        final = wait_until(lambda: jr.status("cycle-1", jobs_dir=jobs).state == "exited" and jr.status("cycle-1", jobs_dir=jobs))
        assert final.returncode == 0
        assert not (jobs / "cycle-1.pid").exists()
        assert json.loads((jobs / "cycle-1.exit.json").read_text(encoding="utf-8"))["returncode"] == 0
        log_text = log.read_text(encoding="utf-8")
        assert "line-one" in log_text and "line-done" in log_text
    finally:
        stop_quietly("cycle-1", jobs)
    missing = jr.status("never-launched", jobs_dir=jobs)
    assert missing.state == "unknown" and missing.pid is None and missing.returncode is None


def test_duplicate_job_id_rejected_until_new_id_minted(sleeper, tmp_path):
    jobs = tmp_path / "jobs"
    first = sleeper("dup-1", 30.0, jobs_dir=jobs)
    try:
        with pytest.raises(jr.JobAlreadyRunningError) as excinfo:
            sleeper("dup-1", 30.0, jobs_dir=jobs)
        assert excinfo.value.pid == first.pid

        retry_cli = run_cli(
            "launch", "--job-id", "dup-1", "--log", str(tmp_path / "again.log"), "--",
            sys.executable, "-c", SLEEPER, "30", jobs_dir=jobs,
        )
        assert retry_cli.returncode == 3
        assert json.loads(retry_cli.stderr)["job_id"] == "dup-1"

        fresh = jr.new_job_id("dup-1")
        assert fresh.startswith("dup-1-") and fresh != "dup-1"
        assert jr.new_job_id("dup-1") != fresh
        second = sleeper(fresh, 30.0, jobs_dir=jobs)
        try:
            assert second.state == "running"
        finally:
            stop_quietly(fresh, jobs)
    finally:
        stop_quietly("dup-1", jobs)


def test_stop_terminates_whole_process_group_with_sigterm(tmp_path):
    jobs = tmp_path / "jobs"
    group_command = ["/bin/bash", "-c", "sleep 30 & sleep 30 & wait"]
    status = jr.launch(group_command, "group-1", tmp_path / "group.log", jobs_dir=jobs)
    pgid = status.pid
    try:
        members = wait_until(lambda: jr.process_group_members(pgid) if len(jr.process_group_members(pgid)) >= 3 else None)
        result = jr.stop("group-1", timeout=5.0, jobs_dir=jobs)
        assert result.state == "exited"
        assert result.stopped_by == "sigterm"
        assert wait_until(lambda: jr.process_group_members(pgid) == [])
        for member in members:
            assert not jr._process_alive(member), f"pid {member} survived stop()"
        assert not (jobs / "group-1.pid").exists()
        assert json.loads((jobs / "group-1.exit.json").read_text(encoding="utf-8"))["returncode"] is None
    finally:
        stop_quietly("group-1", jobs)


def test_stop_escapes_sigterm_ignore_via_sigkill(tmp_path):
    jobs = tmp_path / "jobs"
    stubborn = (
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print('armed', flush=True)\n"
        "time.sleep(60)\n"
    )
    jr.launch([sys.executable, "-c", stubborn], "stubborn-1", tmp_path / "stubborn.log", jobs_dir=jobs)
    try:
        wait_until(lambda: (tmp_path / "stubborn.log").exists() and "armed" in (tmp_path / "stubborn.log").read_text(encoding="utf-8"))
        started = time.monotonic()
        result = jr.stop("stubborn-1", timeout=1.0, jobs_dir=jobs)
        elapsed = time.monotonic() - started
        assert result.state == "exited"
        assert result.stopped_by == "sigkill"
        assert elapsed >= 1.0
        final = wait_until(lambda: jr.status("stubborn-1", jobs_dir=jobs).state == "exited" and jr.status("stubborn-1", jobs_dir=jobs))
        assert final.returncode is None
    finally:
        stop_quietly("stubborn-1", jobs)
