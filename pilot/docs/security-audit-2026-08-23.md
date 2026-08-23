# SandBoxer KVM Match Sandbox — Defensive Security Audit (2026-08-23)

Scope: `pilot/sandboxer_v0/local_kvm.py` (QEMU invocation),
`pilot/sandboxer_v0/runner_tool_server.py` (tool whitelist),
`pilot/scripts/run_command_code_match.py` (match harness).
Method: static review of the three files + host live state (`iptables -L -n`,
`ip link show`). No QEMU was launched; no firewall rules were changed.

## 1. NETWORK ISOLATION — PASS

The runner NIC is a TAP device inside a per-runner, disposable Linux network
namespace — **not** user-mode slirp, and with **no** `hostfwd` anywhere
(repo-wide grep for `hostfwd` matches only the negative assertion in
`tests/test_local_kvm_provider.py:276`).

Evidence (`local_kvm.py`, `_qemu_command`, lines 873–874):

```python
"-nic", "none",
"-netdev", f"tap,id=arena0,ifname={tap},script=no,downscript=no",
"-device", f"virtio-net-pci,netdev=arena0,mac={mac_address}",
```

- QEMU runs via `"ip", "netns", "exec", namespace` + `setpriv --reuid=<qemu_user>`
  (lines 865–866), so the whole guest network stack is born inside an isolated
  namespace.
- Internet / VPS host / host LAN: Blue phase has no default route by
  construction, and it is *witnessed*, not assumed (`_measure_network`, lines
  931–944): `("ip", "-n", record.blue_namespace, "route", "show", "default")`
  must return empty, else `NetworkObservation(..., True, True, True)` fails
  closed via `_require_blue_network_proofs`
  (`LOCAL_KVM_BLUE_EGRESS_DEFAULT_ROUTE_WITNESS_FAILED`, lines 1005–1008), plus
  active guest-side probes: ICMP denied, alternate port denied,
  `orchestrator_denied` (lines 995–1008). Preflight re-checks
  `PreflightCheck("orchestrator_unreachable", not network.orchestrator_reachable, ...)`
  (line 572).
- Other runners: in Blue each tap lives alone in its own namespace
  ("Separate Linux namespaces are the authoritative Blue boundary.", line 932;
  "There is no bridge shared by the two Runners here.", line 887). In Red both
  taps join a dedicated bridge inside a match-scoped red namespace behind a
  deny-by-default nft ruleset applied as one transaction *before* any tap
  joins (lines 894–910, 919–928):
  `add chain bridge sandboxer forward { type filter hook forward priority 0; policy drop; }`
  permitting only ARP and TCP dport/sport `8080` between exactly the two taps
  (`tcp dport {port}` rules lines 900–905) — verified afterwards by rule and
  counter witnesses (`_measure_network` Red branch, `_red_tcp_failure`).
- Host control plane is non-IP: virtio-serial over a Unix socket
  (lines 875–876); "The Unix control socket is not an IP path and cannot be
  reached by either guest." (lines 987–989).

Host live state (INFO): `Chain FORWARD (policy DROP)` with only
Docker/Tailscale chains; host interfaces are `lo`, `ens3` (public NIC),
`docker0`, `tailscale0`. Sandbox guests never attach to any of these — their
TAPs exist only inside per-match namespaces — so none of these extend guest
reachability even if the guest misbehaves inside its namespace.

Verdict: isolation objective met. Deviation from a literal "slirp" invariant
is recorded as GAP-1 below.

## 2. TOOL WHITELIST BOUNDARY — PASS

A non-whitelisted tool is rejected in the Orchestrator process before any
guest I/O. The boundary is `RunnerToolServer._dispatch`
(`runner_tool_server.py` lines 114–135); `self._execute(tool, arguments)`
(line 137) — which is the callback that reaches the guest through
`provider.execute_tool` → virtio-serial — runs only after every reason check:

```python
if reason is None and (not isinstance(tool, str) or tool not in set().union(*_PHASE_TOOLS.values())):
    reason = "RUNNER_TOOL_DENIED"                       # line 122-123
if reason is None and (phase not in _PHASE_TOOLS or tool not in _PHASE_TOOLS[phase]):
    reason = "RUNNER_TOOL_PHASE_DENIED"                 # line 124-125
...
decision = ToolDecision(self._competitor, phase, str(tool), reason is None, reason)
self._audit(decision)
if reason is not None:
    return self._error(reason)                          # never reaches _execute
```

Defense-in-depth in the match script:
- `PHASE_TOOLS` declared once (`run_command_code_match.py` lines 31–34) and
  passed to the adapter as `allowed_tools=PHASE_TOOLS[name]` (line 296);
  prompts embed the same allowlist via `_allowed_tools_sentence` (lines 81–86).
- Frame monitor tripwire (lines 165–185): only `mcp__runner__*` tools may cross
  the seam; a native tool allowed by the bridge raises
  `CommandCodeError("COMMAND_CODE_NATIVE_TOOL_REJECTED")` (lines 179–185).
- Argument shape validation (`_valid_arguments`, lines 146–177) bounds every
  argument set to exact keys and 32 KiB values; the transport itself rejects
  frames >64 KiB (lines 14, 98, 107–108).

## 3. RESOURCE CAPS — PASS (with INFO notes)

- Memory: QEMU `-m str(self.config.memory_mib)` (line 868; default 512 MiB,
  validated ≥256 at line 427) **and** cgroup v2
  `memory.max = memory_mib * 1024 * 1024` (`create_cgroup` call lines
  783–788, writer lines 383–388), membership + limits re-verified
  (`cgroup_limited`, line 578/315–320).
- CPU: `-smp 1` with config forcing `self.vcpus != 1 → ValueError` (line 427)
  and `cpu.max = f"{vcpus * 100000} 100000"` (line 786) = one full core max.
- Fork bomb: `pids.max = 128` (default line 416, floor 32 at line 429,
  applied lines 783–788).
- Disk exhaustion: workspace is a fresh 64 MB qcow2
  (`qemu-img create ... "64M"`, lines 754–757) so the virtual size caps guest
  writes; base image is mounted `readonly=on,cache=none` (line 871); seed is a
  read-only CD-ROM (line 873). Runaway lifetime is bounded by the TTL:
  `ttl_seconds` default 600/floor 30 (lines 417/429), enforced both inline
  (`_enforce_ttl`, lines 1244–1251) and by a crash-independent systemd
  watchdog (`arm_ttl` → `systemd-run ... local_kvm_watchdog`, lines 322–335).
- INFO (residual): `-serial file:{serial_log}` (line 869) is host-side output
  with no explicit quota; growth within one TTL window is bounded only by
  serial throughput and TTL expiry. Minor; consider rotating/truncating if
  TTLs are ever raised far above defaults.

## 4. ESCAPE SURFACE — PASS (with INFO notes)

No shared-filesystem channel exists in the argv builder:

```python
"-drive", f"file={self.config.base_image},if=virtio,format=qcow2,readonly=on,cache=none",   # 871
"-drive", f"file={workspace},if=virtio,format=qcow2,cache=none,discard=unmap",              # 872
"-drive", f"file={seed},media=cdrom,readonly=on", "-nic", "none",                           # 873
```

- No `-virtfs`, no 9p, no virtio-fs anywhere (grep across `pilot/`: zero
  hits; existing suite also asserts `"-virtfs" not in command`,
  `tests/test_local_kvm_provider.py:276`).
- qcow2 backing files: the workspace overlay is created fresh per runner with
  plain `qemu-img create -q -f qcow2` (no `-b` backing option, lines 754–757);
  the base image is attached directly read-only and pinned by SHA-256 digest +
  profile schema check before use (`_verify_base_image`, lines 1253–1276,
  world-writable rejected at 1260–1262). No untrusted backing-chain path into
  arbitrary host files.
- Serial console is output-only to a log file (`-serial file:{serial_log}`,
  line 869); monitor/display disabled (`-monitor none -display none`, line 869)
  plus `-nodefaults` (no default chardevs) and QEMU sandbox hardening:
  `-sandbox on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny`
  (line 870), `-no-reboot`.
- The virtio-serial control socket lives under a 0700 directory owned by the
  unprivileged `sandboxer-runner` user, pre-launch verified by a dropped-
  identity fork witness (`_verify_control_socket_prelaunch` +
  `verify_socket_access`, lines 838–858, 92–117).
- INFO: `-cpu host` exposes host CPUID features (normal for KVM rehearsal);
  cloud-init seed carries only match id/name/nonce/IPs (`_write_cloud_init`,
  lines 1278–1289) — non-sensitive; provider honestly declares
  `production_ready = False` (line 472).

## 5. TEARDOWN — PASS

Between-match teardown destroys runners and verifies destruction instead of
assuming it (`_destroy_record`, lines 1167–1204):

```python
self._host.close_control(record.control_socket)                    # 1169
if not self._cancel_ttl(record.ttl_token): failures.append("TTL_WATCHDOG_REMAINS")
stopped = self._stop_pid(record.pid, record.process_identity)      # SIGTERM→SIGKILL w/ PID+starttime identity (1206–1213)
... qemu-img check ... CGROUP_REMAINS ... ip netns del ... BLUE_NAMESPACE_REMAINS ...
if not self._remove_root(record.root): ... RUNNER_ROOT_REMAINS / MATCH_ROOT_REMAINS
```

Any unverifiable step yields `TeardownState.QUARANTINED` evidence rather than
success, and `reconcile()` re-sweeps partial artifacts including TTL tokens,
QEMU processes, cgroups, TAPs, namespaces and roots (lines 690–733). The
systemd transient watchdog survives an Orchestrator crash and kills only the
verified cgroup if the PID still has the original start time (lines 324–330).
Harness-side (`run_command_code_match.py` finally block, lines 420–426): all
tool servers are closed (Unix sockets unlinked, `RunnerToolServer.close`),
every runner gets `provider.destroy(...)`, telemetry is closed and the
per-match `/var/tmp/sbx-<hash>` socket root is removed
(`_cleanup_socket_root`). Stale sockets fail closed
(`RuntimeError("RUNNER_TOOL_SOCKET_EXISTS")`, lines 78–79) rather than being
silently reused. Expired runners are destroyed on next touch
(`_enforce_ttl`).

## Findings summary

| # | Checklist item | Verdict |
|---|---|---|
| 1 | Network isolation | PASS |
| 2 | Tool whitelist boundary | PASS |
| 3 | Resource caps | PASS (INFO: serial.log unquota'd within TTL) |
| 4 | Escape surface | PASS (INFO: `-cpu host`; seed metadata benign) |
| 5 | Teardown | PASS |

### GAP-1 (documented deviation): "user-mode networking" invariant

Invariant tested in `tests/test_sandbox_hardening.py::
test_user_mode_networking_used` currently FAILS against the code: networking
is TAP-based (`-netdev tap,id=arena0,...`, line 874), not user-mode slirp.

Not fixed intentionally: switching to slirp is neither safe nor obvious. The
Red phase requires L2 peering between the two runners on `10.77.0.11/.12`
through a bridged namespace with nft ARP/TCP filters (lines 894–928); slirp is
per-VM userspace NAT with no bridge model and would force `hostfwd`-style
redirection back into the design. The security goal behind the invariant —
guest cannot reach host LAN/VPS/internet and exposes no host-forwarded ports —
is met by the current architecture (dedicated netns per runner, empty default
route witnessed, deny-by-default bridge policy, zero `hostfwd`). Recorded as
an accepted deviation; revisit only if a slirp-compatible peering design lands.

## Test coverage added

`pilot/tests/test_sandbox_hardening.py` asserts against `_qemu_command` argv:
no `hostfwd`; `-m <MiB>` present and ≥256; single TAP netdev confined to the
netns (`script=no,downscript=no`); `-nic none`; base image `readonly=on`;
no `-virtfs`. The literal slirp check is encoded as `xfail(strict=True)` tied
to GAP-1 so a future migration cannot pass silently without updating this
audit.
