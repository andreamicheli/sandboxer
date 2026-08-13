# Sandboxer v0 operator procedure

1. Verify the frozen Evidence Bundle and the signed Auditor verdict.
2. Verify every Runner teardown record is `destroyed`; quarantine uncertainty.
3. Run the report review chain and record human approval.
4. Build Replay, report, paper, site, and video from the same bundle hash.
5. Run publication gates and inspect the proposed immutable version.
6. Atomically advance the publication pointer. Never overwrite an older version.
7. To roll back, point to a retained earlier version and record the transition.

No procedure authorizes real targets, unrestricted tools, or bypassing a failed gate.
