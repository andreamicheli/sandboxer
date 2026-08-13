# Dependency and image manifest

- Python dependencies: `pilot/uv.lock`
- Video dependencies: `video/package-lock.json`
- Remotion: `4.0.509`
- Command Code adapter: exact CLI binary SHA-256 is recorded by live preflight
- Runner image: exact QCOW2 digest and capability profile are mandatory runtime inputs
- Release artifacts: every component records the same frozen Evidence Bundle hash
