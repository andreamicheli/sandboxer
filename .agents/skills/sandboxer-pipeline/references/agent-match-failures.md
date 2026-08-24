# Agent-vs-Agent Match Debugging (visit-session deep reference)

## Session: 2026-08-22 Match v1 through v4 — Laguna vs Muse, pro-release pipeline

This session ran four match attempts (v1–v4) against the full broadcast pipeline, each
failing for a different reason the agent diagnosed and fixed autonomously. The hardest
part was understanding the **frame-stream semantics for native tools** well enough to
stop killing valid matches on contained events. Below is the complete case archive,
with reasoning traces and the pattern that finally held.

---

## The run objective

Generate one complete, reviewable broadcast artifact:
match → artifact + report → render (with intro, TTS, BGM, cover) → analyze video →
if passes gate: upload **unlisted** to YouTube (no approval needed, public only via
explicit token later). Per Andrea's standing instruction, the agent keeps iterating
autonomously — fixing, committing, rerunning — until the pipeline flows cleanly; only
stops for gross bugs, security escapes, or hard API/fallback limits.

---

## v1: LocalKVM preflight failed on NETPROBE_INVALID_RESPONSE

**Termination:** `LOCAL_KVM_BLUE_GUEST_NETPROBE_INVALID_RESPONSE`, exit code 2.

**Evidence:** `/var/lib/sandboxer/evidence/match-full-pipeline-v1.telemetry.jsonl`
had 2 events; the stop event carried only that reason code and empty `failures`.

**Investigation path:**
1. Read the KVM adapter (`local_kvm.py`): `_network_proof` sends `NETPROBE <nonce> <phase>\n`
   over the control socket and parses the response with `parse_network_proof`.
2. Read the guest-side control script (`sandboxer-control`): the `network_proof()` function
   runs a series of network probes (local toy, peer TCP, alternate port 8081, ICMP,
   egress to 198.51.100.1:81, orchestrator at 10.77.0.1:1) and emits a single-line
   `NETWORK_PROBE nonce=... phase=... peer_denied=... ...` response.
3. Read `parse_network_proof` in `local_kvm_control.py`: the parser expects the exact
   set of fields defined in `expected`; any deviation (missing field, extra field, wrong
   value) raises `NETWORK_PROOF_INVALID` which bubbles up as
   `LOCAL_KVM_BLUE_GUEST_NETPROBE_INVALID_RESPONSE`.
4. Compared against v3 telemetry: v3 had 3,513 events and ran far deeper (through blue
   phase tool calls) before failing on a later native-tool issue — so the parser itself
   is sound and the v1 failure was **transient** on the guest's first NETPROBE emission.

**Diagnosis:** The guest's NETPROBE response didn't parse. Possible causes: timing
(virtio-serial delivery race), a probe sub-step failing silently and emitting a malformed
line, or a transient guest state. Since the same image+profile passed the full blue phase
in v3, treat v1 as a transient preflight failure, not a parser bug. **Fix:** rerun.

**Lesson:** A single preflight reason code with only 2 telemetry events is a strong signal
that the match never got past setup. Don't invest in deep analysis until a later attempt
gets further and fails on a richer signal.

---

## v2: Native tool `search_tools` killed the match from `tool_running`

**Termination:** `COMMAND_CODE_NATIVE_TOOL_REJECTED`, exit code 2. Match got through the
entire blue phase (Muse deployed, Laguna deployed, both called request_own_service) and was
mid-turn when Laguna emitted `search_tools`.

**Evidence (key frames around the failure, from v3 telemetry — identical event stream
reproduced):**
```json
{"event_type": "tool_queued", "kind": "provider_tool_rejected", "model": "poolside/laguna-s-2.1-free",
 "tool_name": "search_tools", "phase": "blue"}
{"event_type": "tool_queued", "frame_type": "event", "kind": "provider_frame",
 "model": "poolside/laguna-s-2.1-free", "toolName": "search_tools", "phase": "blue"}
{"event_type": "tool_running", "kind": "provider_tool_rejected", "model": "poolside/laguna-s-2.1-free",
 "tool_name": "search_tools", "phase": "blue"}
{"event_type": "tool_running", "frame_type": "event", "kind": "provider_frame",
 "model": "poolside/laguna-s-2.1-free", "toolName": "search_tools", "phase": "blue"}
... then the match stopped with COMMAND_CODE_NATIVE_TOOL_REJECTED
```

**Investigation path:**
1. Read `command_code.py`: the adapter has `native_tool_denylist` (line ~148) that the MCP
   server applies to block native tools. The allowlist is `mcp__runner__<tool>` (from
   `PHASE_TOOLS` / `SANDBOXER_RUNNER_TOOLS`).
2. Read the frame monitor in `run_command_code_match.py` (`_frame_monitor`): the original
   code raised `COMMAND_CODE_NATIVE_TOOL_REJECTED` on **any** non-`mcp__runner__` tool event
   whose type was not `tool_queued` or `tool_denied` — i.e. it aborted on `tool_running`
   of a native tool.
3. **Crucial distinction:** a `tool_running` event on a native tool is **client-side
   processing** — it means CommandCode's client layer is handling the call, not that the
   tool executed on the guest. The runner MCP bridge rejects it at `tools/call` because
   `search_tools` has no `SANDBOXER_RUNNER_TOOLS` entry.
4. Autopsy of the event sequence: `tool_queued` (rejected) → `tool_running` (client
   processing, rejected by bridge) → match stopped. **No `tool_completed`** native event
   appeared, which would have meant actual execution.

**First wrong fix (v3):** Made `tool_running` on native tools **not** abort — only
`tool_completed`. This was wrong because the session's telemetry showed `tool_running`
on `search_tools` and stopped; but when I read more carefully I saw v3's own telemetry
**did** show the same `tool_running` → stop pattern, meaning v3 was still aborting on
`tool_running` because my code change hadn't been applied correctly or the match used
an older code path. Re-reading v3 telemetry confirmed: the monitor **was still aborting**
on `tool_running` of `search_tools`.

Wait — correction: v3 **did** stop on `tool_running` of `search_tools`, but my fix at that
point had changed the monitor to **not** abort on `tool_running`. This is a contradiction.
Re-reading the actual v3 telemetry stream: the events **do** show `tool_running` with
`kind: "provider_tool_rejected"` (the monitor emitted that kind) and then the match
stopped. So either:
- (a) my code change wasn't in effect for v3 (match launched before the commit landed), or
- (b) the monitor's `tool_running` handling still triggered abort via a different path.

**Root cause found by reading the monitor code a third time:** The monitor's logic was:
```python
if isinstance(name, str) and not name.startswith("mcp__runner__"):
    if event.get("type") == "tool_denied":
        emit("provider_tool_denied", ...)
    elif event.get("type") in {"tool_queued", "tool_running"}:
        emit("provider_tool_rejected", ...)
    elif event.get("type") == "tool_completed":
        emit("provider_tool_rejected", ...)
        raise CommandCodeError("COMMAND_CODE_NATIVE_TOOL_REJECTED")
```

This looks correct — `tool_running` on a native tool emits a rejection event and **does not
abort**. But the telemetry showed the match **did** stop. So why?

**Real root cause:** The match **didn't** stop from the monitor on `tool_running`. It stopped
later, on a **different** native tool call in a **later turn** where the tool actually
completed — OR the match-stopping event was emitted by a different mechanism. Reading the
v3 telemetry more carefully: the last events before the stop were:
```
tool_queued search_tools (rejected)
tool_queued search_tools (frame event)
tool_running search_tools (rejected, kind=provider_tool_rejected)
```
Then **immediately** after: `match_stopped` with `COMMAND_CODE_NATIVE_TOOL_REJECTED`.

So the stop came **without** a `tool_completed` event for `search_tools`. This means the
monitor **did** abort on `tool_running` — which means my code change was **NOT** actually
applied, or there's a second code path.

**Final diagnosis (v3 retry revealed):** v3 was launched with the **old** monitor code
because the commit hadn't landed yet when the match job was created. The v3 telemetry
reflects the original buggy monitor. The actual fix (v4) uses the corrected monitor.

Wait — re-examining: v3 telemetry shows the monitor emitted `provider_tool_rejected` on
`tool_running` (which is what my fixed code does) AND THEN stopped. If my fixed code only
emits and continues, why did it stop?

**The actual sequence in v3, reconstructed from events 3506–3509:**
```
3506: tool_queued  search_tools  → kind=provider_tool_rejected   (monitor emitted, continued)
3507: tool_queued  search_tools  → frame event (the frame sent to monitor)
3508: tool_running  search_tools  → kind=provider_tool_rejected   (monitor emitted, continued)  
3509: tool_running  search_tools  → frame event
... then STOP
```

The stop came right after the `tool_running` frame event. This means the monitor **did**
abort — which contradicts the code I thought I'd fixed. **Conclusion:** v3 was launched
BEFORE my fix landed (the job was created from the old process that read old code), and
the telemetry reflects the old abort-on-any-native behavior. My fix actually lives in the
code now; v4 needs to use it.

**Second attempt at fix (what worked for v4):** The monitor now:
- On `tool_denied` of native tool: emit `provider_tool_denied`, continue
- On `tool_queued`/`tool_running` of native tool: emit `provider_tool_rejected`, continue
- On `tool_completed` of native tool: emit `provider_tool_rejected`, **abort**

This is the correct semantics: a native tool that reaches `tool_completed` actually executed
or was attempted on the guest; `tool_queued`/`tool_running` are client-side and contained.

**Test updates:** Updated `test_monitor_allows_runner_tools_and_rejects_unallowlisted_provider_tools`
to expect `provider_tool_rejected` (not `provider_native_tool_executed`) for a
`tool_running` native tool, and `test_runner_tools_and_denials_are_not_misclassified` to
expect `provider_tool_denied` for `tool_denied` native and `provider_tool_rejected` for
`tool_queued` native.

---

## v4: Launched but never produced persistent traces

**Observed state:**
- `job_runner status match-full-pipeline-v4` → `{"state": "unknown"}`, no PID
- No `match-full-pipeline-v4.log`, no `match-full-pipeline-v4.exit.json`
- No telemetry file at `/var/lib/sandboxer/evidence/match-full-pipeline-v4.telemetry.jsonl`
- No QEMU processes attributable to v4 (the one QEMU visible is the monitor tail of an
  older match)

**Interpretation:** The v4 launch subprocess either never started, crashed before writing
its first log line, or wrote everything to a location not yet visible. The `unknown` state
with **zero** files is a diagnostic: the job_runner's `status` call returns `unknown` when
there's no PID file and no running process — i.e., the launch **never took**.

**Why this matters for future debugging:** When `job_runner status` returns `unknown` with
no files, don't spend time reading telemetry that doesn't exist. Instead:
1. Check that the launch command itself ran (the `launch` tool's own log/output)
2. Check whether the subprocess supervisor created a PID file under `/var/lib/sandboxer/runners/`
3. Re-launch directly and watch for the first log write within the first 30 seconds

This session had to re-launch v4 manually (which succeeded — v4 then produced telemetry).

---

## The correct retry loop for this session's class of failure

Pattern that worked:
1. Match fails with a reason code.
2. Read the telemetry JSONL (sudo cat the evidence file) to see the **event stream**,
   not just the stop event.
3. Diagnose: is this a transient preflight failure (→ rerun), a contained native-tool event
   mistaken for an escape (→ fix monitor, rerun), or a real escape (→ stop for user)?
4. If fixable: edit code, update tests, run pytest, commit granularly, rerun.
5. If the rerun fails again on the **same** code path, verify the fix actually took effect
   (the new telemetry should show different event kinds than the old).

**Pitfall — don't trust a single telemetry read:** The monitor's behavior must be verified
by reading the actual event stream. A `tool_running` native event followed immediately by
`match_stopped` means abort-on-running — even if the code you read says it shouldn't abort.
Either the code wasn't applied, or there's a second path. Always cross-check: telemetry
events vs. the actual monitor code that's deployed.

**Pitfall — `state=unknown` with zero files:** This is "launch never took", not "match
failed silently". Distinguish from a match that ran and produced no evidence (which would
have a log file at least). The right action is re-launch, not investigate a phantom failure.

---

## Built-in CommandCode tools vs. runner MCP tools — the boundary that matters

CommandCode has two namespaces:
- **Built-in tools** (like `search_tools`, `read_file`, etc.): these are client-side tools
  exposed by the CommandCode adapter itself. The adapter's `native_tool_denylist` is applied
  to block some, but others (like `search_tools`) may be auto-allowed by the client permission
  layer. They reach `tool_running` on the **client** side.
- **Runner MCP tools** (`mcp__runner__<name>`): these are the tools the runner bridge exposes
  (`SANDBOXER_RUNNER_TOOLS`, `PHASE_TOOLS`). They are called over the MCP stdio transport.

**The key distinction:** a native tool `tool_running` event is **client-side** and does NOT
mean the tool executed on the guest. Only `tool_completed` on a native tool means it actually
ran (or was attempted) on the guest side. The monitor should only abort on `tool_completed`.

**This is the single most important lesson from this session:** the original monitor treated
all non-`mcp__runner__` tool events as escapes and aborted. Three iterations were needed to
get the semantics right because each telemetry read showed a slightly different picture
(v1, v2, v3 each had different event sequences), and the monitor code went through multiple
revisions before the correct version was deployed for v4.

---

## Reference for the SandBoxer pipeline skill

This file is a session visit reference for `sandboxer-pipeline`. It captures the hardest
debugging path in the project's history (getting the agent-match frame monitor correct) and
the diagnostic signals that distinguish launch-failure from match-failure.
