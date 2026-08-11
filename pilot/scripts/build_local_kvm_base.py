#!/usr/bin/env python3
"""Build the immutable Alpine Runner base in a temporary, controlled VM.

The build VM is the only place where root writes are allowed.  Runtime Runner
VMs receive the resulting qcow2 as a read-only root disk and only a separate
writable workspace plus a read-only CIDATA metadata disk.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


BASE_URL = "https://dl-cdn.alpinelinux.org/alpine/v3.24/releases/cloud/generic_alpine-3.24.1-x86_64-bios-cloudinit-r0.qcow2"
BASE_SHA256 = "6e2e6fe0572b6632527f268d3659e8fccebda4e1ee470fafe2c4d7b85b6a4df6"
IMAGE_DIRECTORY = Path(__file__).parents[1] / "local_kvm_image"
TEMPLATE_NAMES = (
    "sandboxer-common",
    "sandboxer-mount-runtime",
    "sandboxer-setup",
    "sandboxer-toy",
    "sandboxer-control",
    "sandboxer-runner.init",
    "provision-image",
)
POST_BUILD_SANITIZATION_PATHS = ("/var/lib/cloud", "/run/sandboxer-build")


def sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def templates() -> dict[str, str]:
    return {name: (IMAGE_DIRECTORY / name).read_text(encoding="utf-8") for name in TEMPLATE_NAMES}


def runtime_contract() -> dict[str, str]:
    return {
        "root_filesystem": "readonly",
        "workspace_mount": "/workspace",
        "control_protocol": "virtio-serial-v1",
        "toy_service": "synthetic-http",
    }


def post_build_sanitization() -> dict[str, object]:
    return {"remove_paths": list(POST_BUILD_SANITIZATION_PATHS), "verify_absent": True}


def build_plan(output: Path) -> dict[str, object]:
    return {
        "base_url": BASE_URL,
        "base_sha256": BASE_SHA256,
        "output": os.fspath(output),
        "profile": os.fspath(output.with_suffix(".profile.json")),
        "manifest": os.fspath(output.with_suffix(".evidence.json")),
        "runtime_contract": runtime_contract(),
        "post_build_sanitization": post_build_sanitization(),
    }


def render_cloud_config() -> str:
    entries: list[str] = ["#cloud-config", "write_files:"]
    for name, content in templates().items():
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        entries.extend((
            f"  - path: /run/sandboxer-build/{name}",
            "    permissions: '0700'",
            "    encoding: b64",
            f"    content: {encoded}",
        ))
    entries.extend(("runcmd:", "  - [ /run/sandboxer-build/provision-image ]", ""))
    return "\n".join(entries)


def render_only(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    rendered = templates()
    for name, content in rendered.items():
        (destination / name).write_text(content, encoding="utf-8")
    (destination / "builder-user-data.yaml").write_text(render_cloud_config(), encoding="utf-8")
    (destination / "render-manifest.json").write_text(json.dumps({
        "base_url": BASE_URL,
        "base_sha256": BASE_SHA256,
        "template_sha256": {name: hashlib.sha256(content.encode()).hexdigest() for name, content in rendered.items()},
        "runtime_contract": runtime_contract(),
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def require(command: str) -> None:
    if shutil.which(command) is None:
        raise RuntimeError(f"BUILDER_PREREQUISITE_MISSING:{command}")


def run(argv: tuple[str, ...], *, cwd: Path | None = None) -> None:
    completed = subprocess.run(argv, cwd=cwd, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"BUILDER_COMMAND_FAILED:{argv[0]}:{completed.stderr[-500:]}")


def fetch_base(target: Path) -> None:
    if not target.exists():
        run(("curl", "--fail", "--location", "--proto", "=https", "--tlsv1.2", "--output", os.fspath(target), BASE_URL))
    if sha256(target) != BASE_SHA256:
        raise RuntimeError("BUILDER_BASE_DIGEST_MISMATCH")


def wait_for_build(process: subprocess.Popen[bytes], serial_log: Path, timeout_seconds: int) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        serial = serial_log.read_text(encoding="utf-8", errors="replace") if serial_log.exists() else ""
        if "SANDBOXER_BUILD_OK" in serial:
            break
        if process.poll() is not None:
            raise RuntimeError(f"BUILDER_VM_EXITED:{serial[-1000:]}")
        time.sleep(1)
    else:
        raise RuntimeError("BUILDER_VM_TIMEOUT")
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired as error:
        process.kill()
        raise RuntimeError("BUILDER_VM_DID_NOT_SHUTDOWN") from error
    if process.returncode != 0:
        raise RuntimeError("BUILDER_VM_FAILED_AFTER_MARKER")


def free_nbd_device() -> Path:
    candidates = sorted(Path("/sys/class/block").glob("nbd*"), key=lambda path: path.name)
    for candidate in candidates:
        size = candidate / "size"
        device = Path("/dev") / candidate.name
        if device.exists() and size.read_text(encoding="ascii").strip() == "0":
            return device
    raise RuntimeError("BUILDER_NBD_DEVICE_UNAVAILABLE")


def sanitize_promoted_image(image: Path) -> None:
    """Remove build-time cloud state before an image can be promoted.

    cloud-init may recreate its state after the guest provisioning script has
    completed.  Attach only the unpromoted candidate, edit it in a dedicated
    mount, and verify category absence before the final atomic rename.
    """
    device = free_nbd_device()
    attached = False
    mounted = False
    failure: Exception | None = None
    stage = "ATTACH"
    with tempfile.TemporaryDirectory(prefix="sandboxer-image-sanitize-", dir=image.parent) as directory:
        mountpoint = Path(directory)
        try:
            run(("qemu-nbd", f"--connect={device}", os.fspath(image)))
            attached = True
            stage = "MOUNT"
            run(("mount", "-o", "rw,nosuid,nodev,noexec", os.fspath(device), os.fspath(mountpoint)))
            mounted = True
            stage = "REMOVE"
            for relative_path in POST_BUILD_SANITIZATION_PATHS:
                target = mountpoint / relative_path.lstrip("/")
                if target.is_symlink() or target.is_file():
                    target.unlink()
                elif target.is_dir():
                    shutil.rmtree(target)
            run(("sync",))
            stage = "VERIFY"
            if any((mountpoint / relative_path.lstrip("/")).exists() for relative_path in POST_BUILD_SANITIZATION_PATHS):
                raise RuntimeError("BUILDER_IMAGE_SANITIZATION_UNVERIFIED")
        except Exception as error:
            failure = error
        finally:
            cleanup_failures: list[str] = []
            if mounted:
                completed = subprocess.run(("umount", os.fspath(mountpoint)), text=True, capture_output=True, check=False)
                if completed.returncode != 0:
                    cleanup_failures.append("UNMOUNT")
            if attached:
                completed = subprocess.run(("qemu-nbd", "-d", os.fspath(device)), text=True, capture_output=True, check=False)
                if completed.returncode != 0:
                    cleanup_failures.append("NBD_DISCONNECT")
            if cleanup_failures:
                raise RuntimeError(f"BUILDER_IMAGE_SANITIZATION_CLEANUP_FAILED:{','.join(cleanup_failures)}") from failure
    if failure is not None:
        raise RuntimeError(f"BUILDER_IMAGE_SANITIZATION_FAILED:{stage}") from failure


def build(output: Path, *, cache: Path, timeout_seconds: int) -> dict[str, object]:
    if os.geteuid() != 0:
        raise RuntimeError("BUILDER_REQUIRES_ROOT")
    for command in ("curl", "qemu-img", "qemu-system-x86_64", "cloud-localds", "qemu-nbd", "mount", "umount", "sync"):
        require(command)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() or output.with_suffix(".profile.json").exists() or output.with_suffix(".evidence.json").exists():
        raise RuntimeError("BUILDER_OUTPUT_ALREADY_EXISTS")
    cache.mkdir(parents=True, exist_ok=True)
    base = cache / "generic_alpine-3.24.1-x86_64-bios-cloudinit-r0.qcow2"
    fetch_base(base)
    with tempfile.TemporaryDirectory(prefix="sandboxer-image-build-", dir=output.parent) as temporary:
        work = Path(temporary)
        overlay, seed, serial = work / "build-overlay.qcow2", work / "build-seed.iso", work / "build-serial.log"
        candidate = work / "promoted.qcow2"
        user_data, metadata = work / "user-data.yaml", work / "meta-data"
        user_data.write_text(render_cloud_config(), encoding="utf-8")
        metadata.write_text("instance-id: sandboxer-image-builder\nlocal-hostname: sandboxer-image-builder\n", encoding="ascii")
        run(("qemu-img", "create", "-q", "-f", "qcow2", "-F", "qcow2", "-b", os.fspath(base), os.fspath(overlay)))
        run(("cloud-localds", os.fspath(seed), os.fspath(user_data), os.fspath(metadata)))
        process = subprocess.Popen((
            "qemu-system-x86_64", "-enable-kvm", "-cpu", "host", "-m", "512", "-smp", "1", "-nodefaults",
            "-display", "none", "-serial", f"file:{serial}", "-no-reboot",
            "-drive", f"file={overlay},if=virtio,format=qcow2", "-drive", f"file={seed},media=cdrom,readonly=on",
            "-nic", "user,model=virtio",
        ), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            wait_for_build(process, serial, timeout_seconds)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
        run(("qemu-img", "convert", "-p", "-O", "qcow2", os.fspath(overlay), os.fspath(candidate)))
        sanitize_promoted_image(candidate)
        info = subprocess.run(("qemu-img", "info", "--output=json", os.fspath(candidate)), text=True, capture_output=True, check=False)
        if info.returncode != 0:
            raise RuntimeError("BUILDER_IMAGE_INFO_FAILED")
        image_info = json.loads(info.stdout)
        if image_info.get("backing-filename"):
            raise RuntimeError("BUILDER_IMAGE_HAS_BACKING_FILE")
        candidate.chmod(0o444)
        os.replace(candidate, output)
    info = subprocess.run(("qemu-img", "info", "--output=json", os.fspath(output)), text=True, capture_output=True, check=False)
    if info.returncode != 0:
        raise RuntimeError("BUILDER_IMAGE_INFO_FAILED")
    image_info = json.loads(info.stdout)
    if image_info.get("backing-filename"):
        raise RuntimeError("BUILDER_IMAGE_HAS_BACKING_FILE")
    digest = sha256(output)
    profile = {"schema_version": 1, "image_sha256": digest, **runtime_contract()}
    profile_path = output.with_suffix(".profile.json")
    profile_path.write_text(json.dumps(profile, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    evidence = {
        **build_plan(output), "image_sha256": digest, "image_info": image_info,
        "template_sha256": {name: hashlib.sha256(content.encode()).hexdigest() for name, content in templates().items()},
        "credentials_included": False,
        "post_build_sanitization": post_build_sanitization(),
        "build_mode": "temporary-controlled-vm",
    }
    output.with_suffix(".evidence.json").write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--plan", action="store_true")
    actions.add_argument("--render-only", type=Path)
    actions.add_argument("--build", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("/var/lib/sandboxer/images/runner-alpine-3.24.1.qcow2"))
    parser.add_argument("--cache", type=Path, default=Path("/var/cache/sandboxer"))
    parser.add_argument("--timeout-seconds", type=int, default=300)
    arguments = parser.parse_args(argv)
    try:
        if arguments.plan:
            print(json.dumps(build_plan(arguments.output), indent=2, sort_keys=True))
        elif arguments.render_only:
            render_only(arguments.render_only)
        else:
            print(json.dumps(build(arguments.output, cache=arguments.cache, timeout_seconds=arguments.timeout_seconds), indent=2, sort_keys=True))
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI exercised via subprocess
    raise SystemExit(main())
