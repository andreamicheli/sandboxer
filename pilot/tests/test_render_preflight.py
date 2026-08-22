from __future__ import annotations

import pytest

from sandboxer_v0 import render_preflight as rp
from sandboxer_v0.render_preflight import GiB, preflight_render
from sandboxer_v0.video import remotion_render_command


def _healthy_disk(monkeypatch) -> None:
    """Pretend every path sits on a roomy persistent filesystem."""
    monkeypatch.setattr(rp, "_filesystem_type", lambda path: "ext4")
    monkeypatch.setattr(rp, "_free_bytes", lambda path: 50 * GiB)


def test_low_ram_renders_with_a_single_lane(tmp_path, monkeypatch):
    _healthy_disk(monkeypatch)
    plan = preflight_render(ram_total_bytes=4 * GiB, tmpdir=tmp_path)
    assert plan.concurrency == 1
    assert plan.quality == "low"
    assert plan.warnings == ()
    assert plan.chosen_tmpdir == tmp_path


def test_concurrency_tiers_follow_the_6_and_12_gib_boundaries(tmp_path, monkeypatch):
    _healthy_disk(monkeypatch)
    assert preflight_render(ram_total_bytes=6 * GiB, tmpdir=tmp_path).concurrency == 2
    assert preflight_render(ram_total_bytes=12 * GiB - 1, tmpdir=tmp_path).concurrency == 2
    assert preflight_render(ram_total_bytes=12 * GiB, tmpdir=tmp_path).concurrency == 4
    assert preflight_render(ram_total_bytes=6 * GiB - 1, tmpdir=tmp_path).concurrency == 1


def test_tiny_tmpfs_tmpdir_switches_to_persistent_dir_with_warning(tmp_path, monkeypatch):
    monkeypatch.setattr(rp, "_filesystem_type", lambda path: "tmpfs")
    monkeypatch.setattr(rp, "_free_bytes", lambda path: 50 * GiB)
    persistent = tmp_path / "render-tmp"
    plan = preflight_render(
        ram_total_bytes=16 * GiB, tmpdir=tmp_path / "tmpfs", persistent_root=persistent
    )
    assert plan.chosen_tmpdir == persistent
    assert persistent.is_dir()
    assert plan.requested_tmpdir != plan.chosen_tmpdir
    assert len(plan.warnings) == 1 and "TMPDIR_TMPFS" in plan.warnings[0]
    assert str(plan.requested_tmpdir) in plan.warnings[0]


def test_cramped_persistent_tmpdir_also_switches_with_warning(tmp_path, monkeypatch):
    _healthy_disk(monkeypatch)
    monkeypatch.setattr(rp, "_free_bytes", lambda path: rp._MIN_TMPDIR_FREE_BYTES - 1)
    persistent = tmp_path / "persistent"
    plan = preflight_render(ram_total_bytes=8 * GiB, tmpdir=tmp_path, persistent_root=persistent)
    assert plan.chosen_tmpdir == persistent
    assert persistent.is_dir()
    assert len(plan.warnings) == 1 and "TMPDIR_LOW_SPACE" in plan.warnings[0]


def test_ample_resources_allow_high_concurrency_without_warnings(tmp_path, monkeypatch):
    _healthy_disk(monkeypatch)
    plan = preflight_render(ram_total_bytes=32 * GiB, tmpdir=tmp_path)
    assert plan.concurrency == 4
    assert plan.quality == "high"
    assert plan.tmpdir_is_tmpfs is False
    assert plan.chosen_tmpdir == tmp_path
    assert plan.warnings == ()


def test_default_tmpdir_comes_from_TMPDIR_env(tmp_path, monkeypatch):
    _healthy_disk(monkeypatch)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    plan = preflight_render(ram_total_bytes=8 * GiB)
    assert plan.requested_tmpdir == tmp_path
    assert plan.chosen_tmpdir == tmp_path
    assert plan.warnings == ()


def test_undetectable_ram_fails_closed_to_one_lane_with_warning(tmp_path, monkeypatch):
    _healthy_disk(monkeypatch)
    monkeypatch.setattr(rp, "_ram_total", lambda: None)
    plan = preflight_render(tmpdir=tmp_path)
    assert plan.ram_total_bytes is None
    assert plan.concurrency == 1
    assert any("RAM_UNKNOWN" in warning for warning in plan.warnings)


def test_meminfo_parser_reads_memtotal_into_bytes():
    text = "MemFree:         1024 kB\nMemTotal:       8000000 kB\nSwapTotal:       0 kB\n"
    assert rp._meminfo_ram_bytes(text) == 8_000_000 * 1024
    assert rp._meminfo_ram_bytes("NoMemHere: 12 kB") is None


def test_filesystem_type_finds_the_longest_matching_mount_point():
    mounts = (
        "/dev/sda1 / ext4 rw 0 0\n"
        "tmpfs /dev/shm tmpfs rw 0 0\n"
        "tmpfs /run/user/1000 tmpfs rw 0 0\n"
        "/dev/sdb1 /mnt/my\\040disk btrfs rw 0 0\n"
    )
    assert rp._filesystem_type("/home/ubuntu/x", mounts_text=mounts) == "ext4"
    assert rp._filesystem_type("/dev/shm/remotion", mounts_text=mounts) == "tmpfs"
    assert rp._filesystem_type("/run/user/1000/r", mounts_text=mounts) == "tmpfs"
    assert rp._filesystem_type("/mnt/my disk/r", mounts_text=mounts) == "btrfs"


@pytest.mark.parametrize("lanes", [1, 2, 4])
def test_render_command_builder_passes_plan_concurrency(lanes, tmp_path, monkeypatch):
    ram = {1: 4 * GiB, 2: 8 * GiB, 4: 16 * GiB}[lanes]
    _healthy_disk(monkeypatch)
    plan = preflight_render(ram_total_bytes=ram, tmpdir=tmp_path)
    command = remotion_render_command(output="out.mp4", props="props.json", plan=plan)
    assert command[:5] == ("npx", "remotion", "render", "src/index.tsx", "SandboxerSeries")
    assert command[5] == "out.mp4" and command[6] == "--props=props.json"
    assert f"--concurrency={lanes}" in command


def test_render_command_builder_computes_the_plan_when_none_is_given(tmp_path, monkeypatch):
    _healthy_disk(monkeypatch)
    monkeypatch.setattr(rp, "_ram_total", lambda: 32 * GiB)
    command = remotion_render_command(output="out.mp4", props="props.json")
    assert "--concurrency=4" in command
