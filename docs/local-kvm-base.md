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

The profile is a binding requirement, not evidence of image contents. The
image builder is a separate follow-up. Its acceptance proof must show that
the non-root `competitor` user runs the synthetic toy service and workspace,
the root filesystem is read-only, and the virtio handler actively reports the
Blue/Red network probes. Until that proof exists, `BASE_IMAGE_PROFILE_INVALID`
is the correct fail-closed result and the local adapter must not be marked
production-ready.
