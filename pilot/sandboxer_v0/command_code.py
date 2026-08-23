"""Fail-closed Command Code Model Adapter and schedulability preflight."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

# Client-side provider built-ins that never execute on the guest.  The bridge
# cannot run them (no SANDBOXER_RUNNER_TOOLS entry), so an allow decision for
# one is registry housekeeping, not sandbox execution: record and tolerate.
BENIGN_NATIVE_TOOLS = frozenset({"search_tools"})

THINKING_LOOP_WARNING_SECONDS = 90.0

_TRANSPORT_FAILURE_TOKENS = (
    "overloaded", "unavailable", "timeout", "timed out", "connection", "network",
    "5xx", "bad gateway", "service error", "server error", "internal server error",
)


class CommandCodeError(RuntimeError):
    """A stable, non-sensitive Command Code boundary failure."""

    def __init__(self, reason_code: str, *, model_id: str | None = None) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.model_id = model_id


@dataclass(frozen=True)
class CommandCodeResult:
    requested_model: str
    observed_model: str
    stop_reason: str | None
    turn_count: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    duration_ms: int
    final_text: str
    reasoning_text: str | None
    reasoning_format: str
    event_types: tuple[str, ...]


@dataclass
class CommandCodeBudget:
    """Orchestrator-owned cumulative output accounting across CLI continuations."""

    output_tokens: int
    consumed_output_tokens: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.output_tokens, int) or isinstance(self.output_tokens, bool) or self.output_tokens < 1:
            raise ValueError("a positive output-token budget is required")

    def charge(self, tokens: int) -> None:
        if tokens < 0 or self.consumed_output_tokens + tokens > self.output_tokens:
            raise CommandCodeError("COMMAND_CODE_OUTPUT_BUDGET_EXCEEDED")
        self.consumed_output_tokens += tokens


class ThinkingLoopWatchdog:
    """Observational watchdog for runaway provider thinking spans.

    Episode-v8b m3 died in a thinking loop that burned the whole phase
    timeout.  This tracks consecutive thinking time within one model request
    (a streak is broken by any non-thinking event and reset per request);
    once it passes ``warning_seconds`` the configured ``on_warning`` callback
    receives the elapsed seconds, at most once per streak.  Nothing is ever
    aborted, and per-event cost stays at string comparisons plus two clock
    reads per thinking boundary.
    """

    def __init__(
        self,
        *,
        warning_seconds: float = THINKING_LOOP_WARNING_SECONDS,
        on_warning: Callable[[float], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.warning_seconds = warning_seconds
        self.on_warning = on_warning
        self._clock = clock
        self._reset_streak()

    def _reset_streak(self) -> None:
        self._accumulated = 0.0
        self._span_started_at: float | None = None
        self._warned = False

    def observe(self, event_type: object) -> None:
        if event_type == "thinking_start":
            if self._span_started_at is None:
                self._span_started_at = self._clock()
        elif event_type == "thinking_end":
            started = self._span_started_at
            if started is not None:
                self._span_started_at = None
                self._accumulated += max(0.0, self._clock() - started)
                if not self._warned and self._accumulated > self.warning_seconds:
                    self._warned = True
                    if self.on_warning is not None:
                        self.on_warning(self._accumulated)
        elif event_type in {"thinking_delta", "thinking_text"}:
            return
        else:
            self._reset_streak()

    def reset(self) -> None:
        self._reset_streak()


@dataclass(frozen=True)
class CommandCodePreflight:
    adapter_version: str
    binary_sha256: str
    cli_version: str
    account_fingerprint: str
    catalog_hash: str
    exact_models: tuple[str, ...]
    accounting_categories: tuple[str, ...]
    concurrency_limit: int
    credit_allowance: float
    policy_compatible: bool
    entitled_models: tuple[str, ...]
    session_controls: tuple[str, ...]
    live_probe_models: tuple[str, ...]

    @property
    def snapshot_hash(self) -> str:
        payload = {
            "adapter_version": self.adapter_version, "binary_sha256": self.binary_sha256,
            "cli_version": self.cli_version, "account_fingerprint": self.account_fingerprint,
            "catalog_hash": self.catalog_hash, "exact_models": self.exact_models,
            "accounting_categories": self.accounting_categories, "concurrency_limit": self.concurrency_limit,
            "credit_allowance": self.credit_allowance, "policy_compatible": self.policy_compatible,
            "entitled_models": self.entitled_models, "session_controls": self.session_controls,
            "live_probe_models": self.live_probe_models,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", self.binary_sha256) or not re.fullmatch(r"[0-9a-f]{64}", self.catalog_hash):
            raise CommandCodeError("COMMAND_CODE_PREFLIGHT_DIGEST_INVALID")
        if not self.account_fingerprint or self.concurrency_limit < 2 or self.credit_allowance <= 0:
            raise CommandCodeError("COMMAND_CODE_COMPARABILITY_UNKNOWN")
        required = {"inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens"}
        if set(self.accounting_categories) != required:
            raise CommandCodeError("COMMAND_CODE_ACCOUNTING_ASYMMETRIC")
        if not self.policy_compatible:
            raise CommandCodeError("COMMAND_CODE_POLICY_INCOMPATIBLE")
        if not set(self.exact_models).issubset(self.entitled_models):
            raise CommandCodeError("COMMAND_CODE_ENTITLEMENT_MISSING")
        required_controls = {"no_session", "no_update", "no_skills", "skip_onboarding", "dont_ask"}
        if set(self.session_controls) != required_controls:
            raise CommandCodeError("COMMAND_CODE_SESSION_CONTROLS_UNSUPPORTED")

    def require_pair(self, requested_models: tuple[str, str], *, expected_catalog_hash: str) -> None:
        if tuple(sorted(requested_models)) != tuple(sorted(self.live_probe_models)):
            raise CommandCodeError("COMMAND_CODE_LIVE_PREFLIGHT_REQUIRED")
        if self.catalog_hash != expected_catalog_hash:
            raise CommandCodeError("COMMAND_CODE_CATALOG_DRIFT")
        if len(set(requested_models)) != 2 or any(model not in self.exact_models for model in requested_models):
            raise CommandCodeError("COMMAND_CODE_EXACT_MODEL_MISSING")

    @classmethod
    def from_observations(
        cls, *, binary: Path, cli_version: str, account_identifier: str,
        catalog: Sequence[str], accounting_categories: Sequence[str], concurrency_limit: int | None,
        credit_allowance: float | None, policy_compatible: bool | None,
        entitled_models: Sequence[str], session_controls: Sequence[str],
        live_probe_models: Sequence[str],
    ) -> "CommandCodePreflight":
        if not binary.is_file(): raise CommandCodeError("COMMAND_CODE_BINARY_MISSING")
        if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", cli_version): raise CommandCodeError("COMMAND_CODE_VERSION_UNSUPPORTED")
        if not account_identifier: raise CommandCodeError("COMMAND_CODE_AUTH_REQUIRED")
        if concurrency_limit is None or credit_allowance is None or policy_compatible is None:
            raise CommandCodeError("COMMAND_CODE_COMPARABILITY_UNKNOWN")
        normalized = tuple(sorted(set(catalog)))
        if not normalized or any("/" not in item for item in normalized): raise CommandCodeError("COMMAND_CODE_CATALOG_INVALID")
        catalog_hash = hashlib.sha256(json.dumps(normalized, separators=(",", ":")).encode()).hexdigest()
        account_fingerprint = hashlib.sha256(("sandboxer-command-code:" + account_identifier).encode()).hexdigest()[:16]
        return cls(
            CommandCodeAdapter.adapter_version, CommandCodeAdapter.binary_sha256(binary), cli_version,
            account_fingerprint, catalog_hash, normalized, tuple(accounting_categories),
            concurrency_limit, credit_allowance, policy_compatible, tuple(sorted(set(entitled_models))),
            tuple(sorted(set(session_controls))),
            tuple(sorted(set(live_probe_models))),
        )


class CommandCodeAdapter:
    """Provider-neutral adapter; native Command Code tools never cross this seam."""

    adapter_version = "sandboxer.command-code.v1"
    native_tool_denylist = (
        "read_file", "read_directory", "read_multiple_files", "write_file", "edit_file",
        "grep", "glob", "shell_command", "powershell", "monitor_command", "monitor_events",
        "shell_tasks", "bash_output", "kill_shell", "web_search", "web_fetch", "agent",
        "task_create", "task_update", "task_list", "task_get", "task_output", "task_stop",
        "cron_create", "cron_list", "cron_delete", "todo_write", "ask_user_question",
        "get_diagnostics", "get_command_code_knowledge", "enter_plan_mode", "exit_plan_mode",
    )

    def __init__(
        self,
        executable: Sequence[str] = ("/usr/local/bin/cmd",),
        *,
        workspace_owner: tuple[int, int] | None = None,
    ) -> None:
        if not executable:
            raise ValueError("an explicit executable is required")
        self._executable = tuple(executable)
        self._workspace_owner = workspace_owner

    def command(self, *, prompt: str, model: str, max_turns: int) -> tuple[str, ...]:
        if not prompt.strip() or "/" not in model or max_turns < 1:
            raise ValueError("prompt, full provider/model id, and positive turns are required")
        return (*self._executable, "-p", prompt, "--model", model, "--max-turns", str(max_turns),
                "--output-format", "json", "--no-session", "--no-auto-update", "--no-skills",
                "--skip-onboarding", "--permission-mode", "dont-ask")

    @staticmethod
    def prepare_workspace(
        root: Path, *, runner_bridge: Sequence[str], runner_socket: Path,
        allowed_tools: Sequence[str],
    ) -> None:
        """Install an ephemeral, deny-by-default Command Code control surface."""
        if not runner_bridge or not runner_socket.is_absolute() or not allowed_tools:
            raise CommandCodeError("COMMAND_CODE_BRIDGE_CONFIGURATION_INVALID")
        tools = tuple(dict.fromkeys(allowed_tools))
        if any(not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", item) for item in tools):
            raise CommandCodeError("COMMAND_CODE_BRIDGE_ALLOWLIST_INVALID")
        config = root / ".commandcode"
        config.mkdir(mode=0o700)
        settings = {
            "permissions": {
                "defaultMode": "dont-ask",
                "allow": [f"mcp__runner__{item}" for item in tools],
                "deny": list(CommandCodeAdapter.native_tool_denylist),
            }
        }
        mcp = {"mcpServers": {"runner": {
            "transport": "stdio", "enabled": True, "command": runner_bridge[0],
            "args": list(runner_bridge[1:]),
            "env": {"SANDBOXER_RUNNER_SOCKET": str(runner_socket),
                    "SANDBOXER_RUNNER_TOOLS": ",".join(tools)},
        }}}
        for path, value in ((config / "settings.json", settings), (root / ".mcp.json", mcp)):
            path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")), encoding="utf-8")
            path.chmod(0o600)

    async def run(
        self, *, prompt: str, model: str, max_turns: int, timeout_seconds: float,
        output_token_budget: int, on_frame: Callable[[Mapping[str, Any]], None] | None = None,
        budget: CommandCodeBudget | None = None,
        runner_socket: Path | None = None, allowed_tools: Sequence[str] = (),
        runner_bridge: Sequence[str] | None = None,
        tool_free: bool = False,
    ) -> CommandCodeResult:
        if timeout_seconds <= 0 or output_token_budget < 1:
            raise ValueError("positive timeout and output budget are required")
        with tempfile.TemporaryDirectory(prefix="sandboxer-command-code-") as temporary:
            root = Path(temporary)
            if runner_socket is None and not tool_free:
                raise CommandCodeError("COMMAND_CODE_BRIDGE_REQUIRED")
            if tool_free:
                if runner_socket is not None or allowed_tools:
                    raise CommandCodeError("COMMAND_CODE_TOOL_FREE_BOUNDARY_INVALID")
                config = root / ".commandcode"
                config.mkdir(mode=0o700)
                (config / "settings.json").write_text(json.dumps({"permissions": {
                    "defaultMode": "dont-ask", "allow": [],
                    "deny": list(self.native_tool_denylist),
                }}, sort_keys=True, separators=(",", ":")), encoding="utf-8")
                (config / "settings.json").chmod(0o600)
            else:
                default_bridge = (sys.executable, str(Path(__file__).with_name("command_code_bridge.py").resolve()))
                self.prepare_workspace(
                    root,
                    runner_bridge=runner_bridge or default_bridge,
                    runner_socket=runner_socket,
                    allowed_tools=allowed_tools,
                )
            if self._workspace_owner is not None:
                uid, gid = self._workspace_owner
                owned = [root, root / ".commandcode", root / ".commandcode/settings.json"]
                if not tool_free:
                    owned.append(root / ".mcp.json")
                for path in owned:
                    os.chown(path, uid, gid)
            process = await asyncio.create_subprocess_exec(
                *self.command(prompt=prompt, model=model, max_turns=max_turns), cwd=root,
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stderr_task = asyncio.create_task(process.stderr.read()) if process.stderr is not None else None
            try:
                frames = await asyncio.wait_for(self._read_stream(process, on_frame), timeout_seconds)
            except TimeoutError as error:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 2)
                except TimeoutError:
                    process.kill(); await process.wait()
                raise CommandCodeError("COMMAND_CODE_TIMEOUT") from error
            except asyncio.CancelledError:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 2)
                except TimeoutError:
                    process.kill(); await process.wait()
                raise
            except Exception:
                if process.returncode is None:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), 2)
                    except TimeoutError:
                        process.kill(); await process.wait()
                raise
            finally:
                if stderr_task is not None:
                    await stderr_task
            try:
                result = self._result(frames, process.returncode, model, output_token_budget, frozenset(allowed_tools))
            except CommandCodeError as error:
                raise CommandCodeError(error.reason_code, model_id=model) from error
            if budget is not None:
                budget.charge(result.output_tokens)
            return result

    @classmethod
    async def _read_stream(
        cls, process: asyncio.subprocess.Process,
        on_frame: Callable[[Mapping[str, Any]], None] | None,
    ) -> list[dict[str, Any]]:
        assert process.stdout is not None
        frames: list[dict[str, Any]] = []
        while raw := await process.stdout.readline():
            frame = cls._parse_line(raw)
            frames.append(frame)
            if on_frame is not None:
                on_frame(frame)
        await process.wait()
        if not frames:
            raise CommandCodeError("COMMAND_CODE_STREAM_EMPTY")
        return frames

    @staticmethod
    def _parse_line(raw: bytes) -> dict[str, Any]:
        try: frame = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise CommandCodeError("COMMAND_CODE_NDJSON_MALFORMED") from error
        if not isinstance(frame, dict) or frame.get("type") not in {"event", "result"}:
            raise CommandCodeError("COMMAND_CODE_FRAME_UNKNOWN")
        return frame

    @staticmethod
    def _parse_lines(stdout: bytes) -> list[dict[str, Any]]:
        frames: list[dict[str, Any]] = []
        for number, raw in enumerate(stdout.splitlines(), 1):
            frames.append(CommandCodeAdapter._parse_line(raw))
        if not frames: raise CommandCodeError("COMMAND_CODE_STREAM_EMPTY")
        return frames

    @staticmethod
    def _result(
        frames: list[dict[str, Any]], exit_code: int, model: str, budget: int,
        allowed_tools: frozenset[str] = frozenset(),
    ) -> CommandCodeResult:
        results = [frame for frame in frames if frame.get("type") == "result"]
        if len(results) != 1 or frames[-1] is not results[0]:
            raise CommandCodeError("COMMAND_CODE_RESULT_INVALID")
        observed: set[str] = set(); events: list[str] = []; reasoning: list[str] = []; turns = 0
        runner_tool_calls: set[str] = set()
        for frame in frames[:-1]:
            event = frame.get("event")
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise CommandCodeError("COMMAND_CODE_EVENT_INVALID")
            kind = event["type"]; events.append(kind)
            if kind.startswith("tool_"):
                tool_name = event.get("toolName") or event.get("tool")
                tool_call_id = event.get("toolCallId")
                prefix = "mcp__runner__"
                if kind == "tool_decision":
                    allowed = event.get("allowed")
                    if isinstance(allowed, bool) and allowed and isinstance(tool_name, str):
                        if tool_name.startswith(prefix) and tool_name[len(prefix):] in allowed_tools:
                            continue
                        # Benign client-side built-ins (registry searches) are
                        # never guest execution: tolerate instead of failing
                        # the match (episode-v8b m2 postmortem).
                        if tool_name in BENIGN_NATIVE_TOOLS:
                            continue
                        if tool_name not in allowed_tools:
                            raise CommandCodeError("COMMAND_CODE_NATIVE_TOOL_REJECTED")
                    continue
                if isinstance(tool_name, str) and tool_name.startswith(prefix) and tool_name[len(prefix):] in allowed_tools:
                    if isinstance(tool_call_id, str):
                        runner_tool_calls.add(tool_call_id)
                elif kind in {"tool_queued", "tool_denied", "tool_running", "tool_completed"} or (
                        isinstance(tool_name, str) and tool_name in BENIGN_NATIVE_TOOLS):
                    continue
                elif not isinstance(tool_call_id, str) or tool_call_id not in runner_tool_calls:
                    raise CommandCodeError("COMMAND_CODE_NATIVE_TOOL_REJECTED")
            if kind in {"model_request_start", "model_request_end"} and isinstance(event.get("model"), str): observed.add(event["model"])
            if kind == "thinking_end" and isinstance(event.get("text"), str): reasoning.append(event["text"])
            if kind == "turn_end": turns = max(turns, _integer(event, "turnNumber"))
        result = results[0]; subtype = result.get("subtype")
        if subtype == "error":
            message = str(result.get("error", "")).lower()
            if any(token in message for token in ("auth", "login", "authenticated")): code = "COMMAND_CODE_AUTH_REQUIRED"
            elif any(token in message for token in ("credit", "balance", "quota")): code = "COMMAND_CODE_CREDITS_INSUFFICIENT"
            elif any(token in message for token in ("rate", "concurrency", "too many") + _TRANSPORT_FAILURE_TOKENS): code = "COMMAND_CODE_CAPACITY_UNAVAILABLE"
            elif any(token in message for token in ("mcp", "tool", "server")): code = "COMMAND_CODE_TOOL_BOUNDARY_FAILURE"
            elif any(token in message for token in ("turn", "max turns")): code = "COMMAND_CODE_TURN_LIMIT"
            else: code = "COMMAND_CODE_PROVIDER_FAILURE"
            raise CommandCodeError(code)
        # Providers may echo a canonical casing (e.g. "Qwen/Qwen3.7-Flash" for
        # a requested "qwen/qwen3.7-flash").  Match case-insensitively; a
        # genuinely different model id still fails this check.
        if {name.casefold() for name in observed} != {model.casefold()}:
            raise CommandCodeError("COMMAND_CODE_MODEL_MISMATCH")
        if subtype not in {"success", "max_turns"} or exit_code != ({"success": 0, "max_turns": 8}[subtype]):
            raise CommandCodeError("COMMAND_CODE_PROVIDER_FAILURE")
        usage = result.get("usage")
        if not isinstance(usage, dict): raise CommandCodeError("COMMAND_CODE_USAGE_MISSING")
        values = {key: _integer(usage, key) for key in ("inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens")}
        if values["outputTokens"] > budget: raise CommandCodeError("COMMAND_CODE_OUTPUT_BUDGET_EXCEEDED")
        text = result.get("finalText")
        if not isinstance(text, str): raise CommandCodeError("COMMAND_CODE_FINAL_TEXT_MISSING")
        tagged = re.match(r"^\s*<think>(.*?)</think>\s*", text, re.S | re.I)
        private = "\n\n".join(reasoning) if reasoning else tagged.group(1) if tagged else None
        clean = text[tagged.end():] if tagged and not reasoning else text
        return CommandCodeResult(model, next(iter(observed)), result.get("stopReason") if isinstance(result.get("stopReason"), str) else None,
            turns, values["inputTokens"], values["outputTokens"], values["cacheReadTokens"], values["cacheWriteTokens"],
            _integer(result, "durationMs"), clean, private, "structured-events" if reasoning else "tagged-final-text" if tagged else "none", tuple(events))

    @staticmethod
    def binary_sha256(path: Path) -> str:
        with path.open("rb") as source: return hashlib.file_digest(source, "sha256").hexdigest()


def _integer(value: Mapping[str, Any], key: str) -> int:
    item = value.get(key)
    if not isinstance(item, int) or isinstance(item, bool) or item < 0:
        raise CommandCodeError("COMMAND_CODE_NUMERIC_FIELD_INVALID")
    return item
