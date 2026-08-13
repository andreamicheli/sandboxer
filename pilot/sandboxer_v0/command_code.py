"""Fail-closed Command Code Model Adapter and schedulability preflight."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


class CommandCodeError(RuntimeError):
    """A stable, non-sensitive Command Code boundary failure."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


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

    @property
    def snapshot_hash(self) -> str:
        payload = {
            "adapter_version": self.adapter_version, "binary_sha256": self.binary_sha256,
            "cli_version": self.cli_version, "account_fingerprint": self.account_fingerprint,
            "catalog_hash": self.catalog_hash, "exact_models": self.exact_models,
            "accounting_categories": self.accounting_categories, "concurrency_limit": self.concurrency_limit,
            "credit_allowance": self.credit_allowance, "policy_compatible": self.policy_compatible,
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

    def require_pair(self, requested_models: tuple[str, str], *, expected_catalog_hash: str) -> None:
        if self.catalog_hash != expected_catalog_hash:
            raise CommandCodeError("COMMAND_CODE_CATALOG_DRIFT")
        if len(set(requested_models)) != 2 or any(model not in self.exact_models for model in requested_models):
            raise CommandCodeError("COMMAND_CODE_EXACT_MODEL_MISSING")

    @classmethod
    def from_observations(
        cls, *, binary: Path, cli_version: str, account_identifier: str,
        catalog: Sequence[str], accounting_categories: Sequence[str], concurrency_limit: int | None,
        credit_allowance: float | None, policy_compatible: bool | None,
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
            concurrency_limit, credit_allowance, policy_compatible,
        )


class CommandCodeAdapter:
    """Provider-neutral adapter; native Command Code tools never cross this seam."""

    adapter_version = "sandboxer.command-code.v1"

    def __init__(self, executable: Sequence[str] = ("/usr/local/bin/cmd",)) -> None:
        if not executable:
            raise ValueError("an explicit executable is required")
        self._executable = tuple(executable)

    def command(self, *, prompt: str, model: str, max_turns: int) -> tuple[str, ...]:
        if not prompt.strip() or "/" not in model or max_turns < 1:
            raise ValueError("prompt, full provider/model id, and positive turns are required")
        return (*self._executable, "-p", prompt, "--model", model, "--max-turns", str(max_turns),
                "--output-format", "json", "--no-session", "--no-auto-update", "--no-skills",
                "--skip-onboarding", "--permission-mode", "plan")

    async def run(
        self, *, prompt: str, model: str, max_turns: int, timeout_seconds: float,
        output_token_budget: int, on_frame: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> CommandCodeResult:
        if timeout_seconds <= 0 or output_token_budget < 1:
            raise ValueError("positive timeout and output budget are required")
        with tempfile.TemporaryDirectory(prefix="sandboxer-command-code-") as temporary:
            root = Path(temporary)
            process = await asyncio.create_subprocess_exec(
                *self.command(prompt=prompt, model=model, max_turns=max_turns), cwd=root,
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout_seconds)
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
            frames = self._parse_lines(stdout)
            if on_frame:
                for frame in frames: on_frame(frame)
            return self._result(frames, process.returncode, model, output_token_budget)

    @staticmethod
    def _parse_lines(stdout: bytes) -> list[dict[str, Any]]:
        frames: list[dict[str, Any]] = []
        for number, raw in enumerate(stdout.splitlines(), 1):
            try: frame = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise CommandCodeError("COMMAND_CODE_NDJSON_MALFORMED") from error
            if not isinstance(frame, dict) or frame.get("type") not in {"event", "result"}:
                raise CommandCodeError("COMMAND_CODE_FRAME_UNKNOWN")
            frames.append(frame)
        if not frames: raise CommandCodeError("COMMAND_CODE_STREAM_EMPTY")
        return frames

    @staticmethod
    def _result(frames: list[dict[str, Any]], exit_code: int, model: str, budget: int) -> CommandCodeResult:
        results = [frame for frame in frames if frame.get("type") == "result"]
        if len(results) != 1 or frames[-1] is not results[0]:
            raise CommandCodeError("COMMAND_CODE_RESULT_INVALID")
        observed: set[str] = set(); events: list[str] = []; reasoning: list[str] = []; turns = 0
        for frame in frames[:-1]:
            event = frame.get("event")
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise CommandCodeError("COMMAND_CODE_EVENT_INVALID")
            kind = event["type"]; events.append(kind)
            if kind.startswith("tool_") or (kind == "turn_end" and event.get("hadToolCalls")):
                raise CommandCodeError("COMMAND_CODE_NATIVE_TOOL_REJECTED")
            if kind in {"model_request_start", "model_request_end"} and isinstance(event.get("model"), str): observed.add(event["model"])
            if kind == "thinking_end" and isinstance(event.get("text"), str): reasoning.append(event["text"])
            if kind == "turn_end": turns = max(turns, _integer(event, "turnNumber"))
        result = results[0]; subtype = result.get("subtype")
        if subtype == "error":
            message = str(result.get("error", "")).lower()
            if any(token in message for token in ("auth", "login", "authenticated")): code = "COMMAND_CODE_AUTH_REQUIRED"
            elif any(token in message for token in ("credit", "balance", "quota")): code = "COMMAND_CODE_CREDITS_INSUFFICIENT"
            elif any(token in message for token in ("rate", "concurrency", "too many")): code = "COMMAND_CODE_CAPACITY_UNAVAILABLE"
            else: code = "COMMAND_CODE_PROVIDER_FAILURE"
            raise CommandCodeError(code)
        if observed != {model}: raise CommandCodeError("COMMAND_CODE_MODEL_MISMATCH")
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
