# Local KVM Runner base

`LocalKvmRunnerProvider` accepts only a prebuilt immutable Runner image. It
attaches that image directly as QEMU's `readonly=on` root disk and creates a
separate disposable `workspace.qcow2` for each Runner. A generic cloud image
is not a valid Runner base: cloud-init cannot establish the required control
or toy-service contract on a read-only root filesystem.

The image must have a sibling JSON profile with exactly these fields:

```json
{
  "schema_version": 1,
  "image_sha256": "the exact SHA-256 configured for this qcow2",
  "root_filesystem": "readonly",
  "workspace_mount": "/workspace",
  "control_protocol": "virtio-serial-v1",
  "toy_service": "synthetic-http"
}
```

The profile is a binding requirement, not evidence of image contents. Build
the local image only through the controlled temporary build environment:

```sh
cd pilot
sudo -n .venv/bin/python scripts/build_local_kvm_base.py --build \
  --output /var/lib/sandboxer/images/runner-alpine-3.24.1.qcow2
```

The builder downloads the pinned Alpine 3.24.1 cloud base, creates a temporary
writable build overlay, installs the locked guest `competitor` (UID 1001),
OpenRC setup/control services, and the synthetic HTTP notes service, then
disables cloud-init and flattens the result into a standalone qcow2. It writes
a digest-bound exact profile and a non-secret evidence manifest next to the
image. Runtime metadata is supplied only through a read-only CIDATA disk;
the immutable root disk is attached with `readonly=on` and `/workspace` is the
separate writable qcow2 disk.

The guest control service actively checks the absence of known host/shared
mount types and provider-credential paths/environment names. Its network probe
actively tests Blue peer denial and Red HTTP access, plus an alternate port,
ICMP, default-route/direct-egress, and an unreachable Orchestrator IP path.
These are containment witnesses, not a proof of adversarial escape resistance.

Run the real two-Runner rehearsal as root because the implementation uses
QEMU/KVM together with host-privileged network namespaces, TAPs, nftables and
cgroups:

```sh
sudo -n env PYTHONPATH="$PWD" .venv/bin/python scripts/rehearse_local_kvm.py \
  --image /var/lib/sandboxer/images/runner-alpine-3.24.1.qcow2 \
  --profile /var/lib/sandboxer/images/runner-alpine-3.24.1.profile.json \
  --match-id local-kvm-rehearsal-001
```

`production_ready` remains `false` even after a successful rehearsal. The
proof demonstrates this configured controlled runtime, not general resistance
to a hostile Runner workload.
