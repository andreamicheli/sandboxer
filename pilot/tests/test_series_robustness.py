"""Series robustness: budget retries, prompt allowlists, strict gate, variety.

Regression coverage for the episode-v8 postmortem (m2 died with
COMMAND_CODE_OUTPUT_BUDGET_EXCEEDED; m3 burned its turns calling the
non-whitelisted search_tools and never completed deploy_service) and for
episode-v8b (m2 died with COMMAND_CODE_NATIVE_TOOL_REJECTED when laguna kept
calling the benign bridge-native search_tools; m3 hit COMMAND_CODE_TIMEOUT in
a thinking loop):

  - the series driver retries exactly one ``*_BUDGET_EXCEEDED`` failure with
    1.5x token budgets, and exactly one ``COMMAND_CODE_TIMEOUT`` failure with
    the same budgets plus a 1.5x ``phase_timeout``, under the derived identity
    ``<seed>-retry1``, recording which attempt produced the data point;
  - both phase prompts enumerate the EXACT ``PHASE_TOOLS`` allowlist and
    state that any other tool name will be rejected;
  - a ``tool_decision`` for a BENIGN_NATIVE_TOOLS entry (search_tools) is
    tolerated with telemetry instead of aborting, while unknown native tools
    stay fatal;
  - runner-tool ``tool_errored`` soft events (e.g. RUNNER_TOOL_ARGUMENTS_INVALID)
    never escalate to a fatal adapter error;
  - consecutive thinking spans past 90s emit an observational
    ``thinking_loop_warning`` without aborting;
  - ``build_real_artifacts.py --series`` refuses to write artifacts unless
    enough matches are valid captures (--require-valid / --force);
  - identical Blue brief families across the episode are flagged as
    ``brief_variety: low`` instead of failing the series.

No test here launches QEMU: the match runner is injected or bypassed.
"""

from __future__ import annotations

import argparse
import json
import re

import pytest

from sandboxer_v0.blue_briefs import brief_manifest, select_blue_briefs
from sandboxer_v0.command_code import (
    BENIGN_NATIVE_TOOLS,
    CommandCodeAdapter,
    CommandCodeError,
    ThinkingLoopWatchdog,
)
from sandboxer_v0.series_result import SERIES_SCHEMA, build_series_summary
import scripts.build_real_artifacts as build_module
from scripts.build_real_artifacts import count_valid_capture_results, main as build_main
from scripts.run_command_code_match import (
    MatchCalibrationError,
    PHASE_TOOLS,
    _blue_prompt,
    _frame_monitor,
    _red_prompt,
)
from scripts.run_series import BUDGET_RETRY_MULTIPLIER, execute_series

MODEL_A = "poolside/laguna-s-2.1-free"
MODEL_B = "meta/muse-spark-1.2-contributor"
MODELS = (MODEL_A, MODEL_B)


# --- scripted injectable runner ------------------------------------------------


class _ScriptedRunner:
    """Returns scripted winners or raises scripted failures, in call order."""

    def __init__(self, script, *, blue_briefs=None):
        self.script = list(script)
        self.blue_briefs = blue_briefs
        self.calls: list[argparse.Namespace] = []
        self.successes = 0

    def __call__(self, args):
        self.calls.append(args)
        step = self.script[len(self.calls) - 1]
        if isinstance(step, BaseException):
            raise step
        payload = {
            "result": "passed",
            "outcome": "VALID_CAPTURE" if step else "VALID_NO_CAPTURE",
            "reason_code": "SOLE_CAPTURE" if step else "NO_CAPTURE_AVAILABILITY",
            "models": list(args.models),
            "match_id": args.match_id,
            "seed": args.seed,
            "winner": step,
            "captures": [step == MODELS[0], step == MODELS[1]],
            "usage": {},
        }
        if self.blue_briefs:
            payload["blue_brief"] = brief_manifest(
                self.blue_briefs[self.successes % len(self.blue_briefs)])
        self.successes += 1
        return payload


def _series_args(evidence_dir, **overrides):
    base = dict(
        image="/tmp/opencode/never-used.qcow2",
        profile="/tmp/opencode/never-used.profile.json",
        series_id="ep-v8",
        seed_prefix=None,
        models=list(MODELS),
        runner_root="/tmp/opencode/runners",
        evidence_dir=evidence_dir,
        ttl_seconds=600,
        phase_timeout=180.0,
        blue_tokens=4096,
        red_tokens=4096,
        interview_tokens=1024,
        blue_turns=4,
        red_turns=5,
        blue_tools=8,
        red_tools=10,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


# --- requirement 1: budget retry ------------------------------------------------


def test_budget_failure_retries_once_with_larger_budget_and_succeeds(tmp_path):
    runner = _ScriptedRunner([
        MODEL_A,
        CommandCodeError("COMMAND_CODE_OUTPUT_BUDGET_EXCEEDED"),
        MODEL_A,
        None,
    ])

    summary = execute_series(_series_args(tmp_path), runner=runner)

    assert len(runner.calls) == 4
    first, retry = runner.calls[1], runner.calls[2]
    assert (first.match_id, first.seed) == ("ep-v8-m2", "ep-v8-m2")
    assert (retry.match_id, retry.seed) == ("ep-v8-m2-retry1", "ep-v8-m2-retry1")
    for field, base_value in (("blue_tokens", 4096), ("red_tokens", 4096),
                              ("interview_tokens", 1024)):
        assert getattr(retry, field) == int(base_value * 1.5), field
    assert retry.blue_turns == first.blue_turns
    assert retry.red_tools == first.red_tools

    recorded = summary["matches"][1]
    assert recorded["result_status"] == "passed"
    assert recorded["match_id"] == "ep-v8-m2-retry1"
    assert recorded["evidence"]["result"].endswith("ep-v8-m2-retry1.result.json")
    attempts = recorded["attempts"]
    assert [entry["attempt"] for entry in attempts] == [1, 2]
    assert attempts[0]["succeeded"] is False
    assert attempts[0]["reason_code"] == "COMMAND_CODE_OUTPUT_BUDGET_EXCEEDED"
    assert set(attempts[0]["budget_multipliers"].values()) == {1.0}
    assert attempts[1]["succeeded"] is True
    assert set(attempts[1]["budget_multipliers"].values()) == {BUDGET_RETRY_MULTIPLIER}

    written = json.loads((tmp_path / "ep-v8.series.json").read_text(encoding="utf-8"))
    assert written["schema_version"] == SERIES_SCHEMA
    assert written["matches"][1]["attempts"] == attempts


def test_non_budget_failures_are_never_retried(tmp_path):
    runner = _ScriptedRunner([
        CommandCodeError("BLUE_PHASE_ACTION_MISSING"),
        MODEL_A,
        MODEL_A,
    ])

    summary = execute_series(_series_args(tmp_path), runner=runner)

    assert [call.match_id for call in runner.calls] == ["ep-v8-m1", "ep-v8-m2", "ep-v8-m3"]
    failed = summary["matches"][0]
    assert failed["result_status"] == "failed"
    assert failed["reason_code"] == "BLUE_PHASE_ACTION_MISSING"
    assert len(failed["attempts"]) == 1
    assert failed["attempts"][0]["reason_code"] == "BLUE_PHASE_ACTION_MISSING"


def test_budget_retry_is_capped_at_one_attempt_and_retry_failure_is_recorded(tmp_path):
    runner = _ScriptedRunner([
        MatchCalibrationError("COMMAND_CODE_OUTPUT_BUDGET_EXCEEDED"),
        CommandCodeError("COMMAND_CODE_OUTPUT_BUDGET_EXCEEDED"),
        MODEL_A,
        None,
    ])

    summary = execute_series(_series_args(tmp_path), runner=runner)

    assert [call.match_id for call in runner.calls] == [
        "ep-v8-m1", "ep-v8-m1-retry1", "ep-v8-m2", "ep-v8-m3"]
    failed = summary["matches"][0]
    assert failed["result_status"] == "failed"
    assert failed["reason_code"] == "COMMAND_CODE_OUTPUT_BUDGET_EXCEEDED"
    assert failed["match_id"] == "ep-v8-m1-retry1"
    assert [entry["succeeded"] for entry in failed["attempts"]] == [False, False]
    assert failed["evidence"]["telemetry"].endswith("ep-v8-m1-retry1.telemetry.jsonl")
    assert summary["matches"][1]["result_status"] == "passed"
    assert summary["matches"][2]["result_status"] == "passed"


def test_timeout_failure_retries_once_with_larger_budget_and_phase_timeout(tmp_path):
    """Episode-v8b m3: a thinking-loop timeout gets one retry with 1.5x token
    budgets AND a 1.5x phase_timeout under the derived retry identity."""
    runner = _ScriptedRunner([
        CommandCodeError("COMMAND_CODE_TIMEOUT"),
        MODEL_A,
        MODEL_A,
        None,
    ])

    summary = execute_series(_series_args(tmp_path), runner=runner)

    assert len(runner.calls) == 4
    first, retry = runner.calls[0], runner.calls[1]
    assert (first.match_id, first.seed) == ("ep-v8-m1", "ep-v8-m1")
    assert (retry.match_id, retry.seed) == ("ep-v8-m1-retry1", "ep-v8-m1-retry1")
    for field, base_value in (("blue_tokens", 4096), ("red_tokens", 4096),
                              ("interview_tokens", 1024)):
        assert getattr(retry, field) == int(base_value * BUDGET_RETRY_MULTIPLIER), field
    assert retry.phase_timeout == pytest.approx(first.phase_timeout * BUDGET_RETRY_MULTIPLIER)
    assert retry.phase_timeout == pytest.approx(180.0 * 1.5)
    assert retry.blue_turns == first.blue_turns and retry.red_tools == first.red_tools

    recorded = summary["matches"][0]
    assert recorded["result_status"] == "passed"
    assert recorded["match_id"] == "ep-v8-m1-retry1"
    attempts = recorded["attempts"]
    assert [entry["attempt"] for entry in attempts] == [1, 2]
    assert attempts[0]["succeeded"] is False
    assert attempts[0]["reason_code"] == "COMMAND_CODE_TIMEOUT"
    assert set(attempts[0]["budget_multipliers"].values()) == {1.0}
    assert attempts[1]["succeeded"] is True
    assert set(attempts[1]["budget_multipliers"].values()) == {BUDGET_RETRY_MULTIPLIER}
    # The other two matches were never retried.
    assert [call.match_id for call in runner.calls[2:]] == ["ep-v8-m2", "ep-v8-m3"]
    assert summary["matches"][1]["result_status"] == "passed"
    assert summary["matches"][2]["result_status"] == "passed"


def test_timeout_retry_is_capped_at_one_attempt_and_failure_is_recorded(tmp_path):
    runner = _ScriptedRunner([
        MatchCalibrationError("COMMAND_CODE_TIMEOUT"),
        CommandCodeError("COMMAND_CODE_TIMEOUT"),
        MODEL_A,
        None,
    ])

    summary = execute_series(_series_args(tmp_path), runner=runner)

    assert [call.match_id for call in runner.calls][:2] == ["ep-v8-m1", "ep-v8-m1-retry1"]
    failed = summary["matches"][0]
    assert failed["result_status"] == "failed"
    assert failed["reason_code"] == "COMMAND_CODE_TIMEOUT"
    assert failed["match_id"] == "ep-v8-m1-retry1"
    assert [entry["succeeded"] for entry in failed["attempts"]] == [False, False]


# --- requirement 2 (episode-v8b m2): benign native tools -------------------------


def _success_frames(*events: dict) -> list[dict]:
    return [
        {"type": "event", "event": {"type": "model_request_start", "model": "example/model"}},
        *events,
        {"type": "event", "event": {"type": "turn_end", "turnNumber": 1}},
        {"type": "event", "event": {"type": "model_request_end", "model": "example/model"}},
        {"type": "result", "subtype": "success", "stopReason": "end_turn",
         "usage": {"inputTokens": 10, "outputTokens": 5, "cacheReadTokens": 2, "cacheWriteTokens": 0},
         "durationMs": 20, "finalText": "ok"},
    ]


def test_benign_native_tool_decision_is_tolerated_with_telemetry():
    """Episode-v8b m2: search_tools is a bridge-native, read-only registry
    helper; an allow decision must never abort the match."""
    assert BENIGN_NATIVE_TOOLS == frozenset({"search_tools"})

    emitted = []
    monitor = _frame_monitor(MODEL_A, "blue", emit=lambda kind, **fields: emitted.append((kind, fields)))
    monitor({"type": "event", "event": {
        "type": "tool_decision", "toolName": "search_tools", "allowed": True}})

    assert any(
        kind == "provider_native_tool_benign"
        and fields.get("tool_name") == "search_tools"
        and fields.get("model") == MODEL_A
        and fields.get("phase") == "blue"
        for kind, fields in emitted
    )
    assert not any(kind == "provider_native_tool_executed" for kind, _ in emitted)


def test_adapter_result_tolerates_benign_native_tool_decision():
    result = CommandCodeAdapter._result(
        _success_frames({"type": "event", "event": {
            "type": "tool_decision", "toolName": "search_tools", "allowed": True}}),
        0, "example/model", 100, frozenset(PHASE_TOOLS["blue"]),
    )
    assert "tool_decision" in result.event_types


def test_unknown_native_tool_decision_still_raises():
    emitted = []
    monitor = _frame_monitor(MODEL_A, "red", emit=lambda kind, **fields: emitted.append((kind, fields)))

    with pytest.raises(CommandCodeError) as raised:
        monitor({"type": "event", "event": {
            "type": "tool_decision", "toolName": "shell_command", "allowed": True}})

    assert raised.value.reason_code == "COMMAND_CODE_NATIVE_TOOL_REJECTED"
    assert any(
        kind == "provider_native_tool_executed" and fields.get("tool_name") == "shell_command"
        for kind, fields in emitted
    )
    assert not any(kind == "provider_native_tool_benign" for kind, _ in emitted)

    with pytest.raises(CommandCodeError, match="COMMAND_CODE_NATIVE_TOOL_REJECTED"):
        CommandCodeAdapter._result(
            _success_frames({"type": "event", "event": {
                "type": "tool_decision", "toolName": "shell_command", "allowed": True}}),
            0, "example/model", 100, frozenset(PHASE_TOOLS["red"]),
        )


def test_runner_tool_errored_soft_event_is_never_fatal():
    """RUNNER_TOOL_ARGUMENTS_INVALID surfaces as a tool_errored soft event;
    the adapter must record it without escalating to a fatal error."""
    frames = _success_frames(
        {"type": "event", "event": {"type": "tool_decision", "tool": "http_request", "allowed": True}},
        {"type": "event", "event": {
            "type": "tool_running", "toolName": "mcp__runner__http_request", "toolCallId": "call-7"}},
        {"type": "event", "event": {
            "type": "tool_errored", "toolName": "mcp__runner__http_request", "toolCallId": "call-7"}},
    )

    result = CommandCodeAdapter._result(frames, 0, "example/model", 100, frozenset({"http_request"}))

    assert "tool_errored" in result.event_types


# --- requirement 5 (episode-v8b m3): thinking watchdog ---------------------------


def test_thinking_watchdog_warns_once_past_ninety_consecutive_seconds():
    clock = [0.0]
    warnings = []
    watchdog = ThinkingLoopWatchdog(on_warning=warnings.append, clock=lambda: clock[0])

    watchdog.observe("model_request_start")
    watchdog.observe("thinking_start")
    clock[0] = 95.0
    watchdog.observe("thinking_end")

    assert len(warnings) == 1
    assert warnings[0] == pytest.approx(95.0)


def test_thinking_watchdog_sums_back_to_back_spans_within_one_request():
    clock = [0.0]
    warnings = []
    watchdog = ThinkingLoopWatchdog(on_warning=warnings.append, clock=lambda: clock[0])

    watchdog.observe("model_request_start")
    for _ in range(3):
        watchdog.observe("thinking_start")
        clock[0] += 40.0
        watchdog.observe("thinking_end")

    assert warnings == [pytest.approx(120.0)]


def test_thinking_watchdog_ignores_short_or_non_consecutive_thinking():
    clock = [0.0]
    warnings = []
    watchdog = ThinkingLoopWatchdog(on_warning=warnings.append, clock=lambda: clock[0])

    watchdog.observe("thinking_start")
    clock[0] = 30.0
    watchdog.observe("thinking_end")
    watchdog.observe("text_delta")  # breaks the consecutive streak
    watchdog.observe("thinking_start")
    clock[0] = 50.0
    watchdog.observe("thinking_end")

    assert warnings == []


def test_thinking_watchdog_resets_per_model_request():
    clock = [0.0]
    warnings = []
    watchdog = ThinkingLoopWatchdog(on_warning=warnings.append, clock=lambda: clock[0])

    for request in (1, 2):
        watchdog.observe("model_request_start")
        watchdog.observe("thinking_start")
        clock[0] = 95.0 * request
        watchdog.observe("thinking_end")

    assert len(warnings) == 2


def test_frame_monitor_emits_thinking_loop_warning_without_aborting():
    ticks = iter([0.0, 120.0])
    emitted = []
    fast_watchdog = ThinkingLoopWatchdog(warning_seconds=90.0, clock=lambda: next(ticks))
    monitor = _frame_monitor(MODEL_B, "blue", emit=lambda kind, **fields: emitted.append((kind, fields)),
                             watchdog=fast_watchdog)

    monitor({"type": "event", "event": {"type": "model_request_start", "model": MODEL_B}})
    monitor({"type": "event", "event": {"type": "thinking_start"}})
    monitor({"type": "event", "event": {"type": "thinking_delta"}})
    monitor({"type": "event", "event": {"type": "thinking_end"}})
    monitor({"type": "event", "event": {"type": "turn_end", "turnNumber": 1}})

    warning_fields = next(fields for kind, fields in emitted if kind == "thinking_loop_warning")
    assert warning_fields["elapsed_seconds"] == pytest.approx(120.0)
    assert warning_fields["model"] == MODEL_B
    assert warning_fields["phase"] == "blue"


# --- requirement 2: prompt hardening --------------------------------------------


def _listed_tools(prompt: str, phase_sentence_prefix: str) -> set[str]:
    match = re.search(re.escape(phase_sentence_prefix) + r"([^.]*)\.", prompt)
    assert match, f"allowlist sentence missing from prompt: {prompt[:120]}..."
    return {name.strip() for name in match.group(1).split(",")}


def test_blue_prompt_lists_exact_phase_allowlist_and_rejection_warning():
    brief = select_blue_briefs("calibration-seed", count=1)[0]
    prompt = _blue_prompt(brief)
    prefix = "Only these tools are available this phase: "
    assert prefix in prompt
    assert "Any other tool name WILL be rejected." in prompt
    listed = _listed_tools(prompt, prefix)
    assert listed == {f"mcp__runner__{tool}" for tool in PHASE_TOOLS["blue"]}


def test_red_prompt_lists_exact_phase_allowlist_and_rejection_warning():
    prompt = _red_prompt("10.77.0.12")
    prefix = "Only these tools are available this phase: "
    assert prefix in prompt
    assert "Any other tool name WILL be rejected." in prompt
    listed = _listed_tools(prompt, prefix)
    assert listed == {f"mcp__runner__{tool}" for tool in PHASE_TOOLS["red"]}
    assert "10.77.0.12:8080" in prompt
    assert prompt != _red_prompt("10.77.0.11")


# --- requirement 3: strict series gate ------------------------------------------


def _result_doc(number: int, *, valid: bool) -> dict:
    if valid:
        return {"result": "passed", "outcome": "VALID_CAPTURE",
                "reason_code": "SOLE_CAPTURE", "models": list(MODELS),
                "winner": MODELS[number % 2], "captures": [True, False],
                "match_id": f"ep-x-m{number}", "usage": {}}
    return {"result": "failed", "outcome": "INVALID", "reason_code": "NO_CAPTURE_AVAILABILITY",
            "models": list(MODELS), "winner": None, "captures": [],
            "match_id": f"ep-x-m{number}", "usage": {}}


@pytest.fixture()
def stub_build(monkeypatch):
    built: list[int] = []

    def fake_build(telemetry_docs, result_docs, **kwargs):
        built.append(len(result_docs))
        return {"replay": {"frames": []}, "report": {},
                "manifest": {"scenes": [{"duration_frames": 30}], "fps": 30,
                             "commentary": []},
                "series": None}

    monkeypatch.setattr(build_module, "build_broadcast_artifacts", fake_build)
    return built


def _write_inputs(tmp_path, results: list[dict], out_dir: str) -> list[str]:
    telemetry_paths = []
    result_paths = []
    for index, document in enumerate(results, start=1):
        telemetry = tmp_path / f"m{index}.telemetry.jsonl"
        telemetry.write_text("\n", encoding="utf-8")
        result = tmp_path / f"m{index}.result.json"
        result.write_text(json.dumps(document), encoding="utf-8")
        telemetry_paths.append(str(telemetry))
        result_paths.append(str(result))
    return ["--series", "--out-dir", out_dir,
            "--telemetry", *telemetry_paths, "--result", *result_paths]


def test_strict_gate_passes_with_two_valid_matches(tmp_path, stub_build, capsys):
    out_dir = str(tmp_path / "out")
    results = [_result_doc(1, valid=True), _result_doc(2, valid=False), _result_doc(3, valid=True)]
    code = build_main(_write_inputs(tmp_path, results, out_dir))
    assert code == 0
    assert stub_build == [3]
    assert (tmp_path / "out" / "replay.json").exists()


def test_strict_gate_fails_with_one_valid_match_before_writing(tmp_path, stub_build, capsys):
    out_dir = str(tmp_path / "out")
    results = [_result_doc(1, valid=True), _result_doc(2, valid=False), _result_doc(3, valid=False)]
    code = build_main(_write_inputs(tmp_path, results, out_dir))
    assert code != 0
    assert "SERIES_INSUFFICIENT_VALID_MATCHES" in capsys.readouterr().err
    assert stub_build == [], "gate must fire before building artifacts"
    assert not (tmp_path / "out" / "replay.json").exists()


def test_force_overrides_the_strict_gate(tmp_path, stub_build, capsys):
    out_dir = str(tmp_path / "out")
    results = [_result_doc(1, valid=True), _result_doc(2, valid=False), _result_doc(3, valid=False)]
    argv = _write_inputs(tmp_path, results, out_dir) + ["--force"]
    code = build_main(argv)
    assert code == 0
    assert stub_build == [3]
    assert (tmp_path / "out" / "replay.json").exists()


def test_require_valid_threshold_is_configurable(tmp_path, stub_build, capsys):
    out_dir = str(tmp_path / "out")
    results = [_result_doc(1, valid=True), _result_doc(2, valid=True), _result_doc(3, valid=False)]
    argv = _write_inputs(tmp_path, results, out_dir) + ["--require-valid", "3"]
    code = build_main(argv)
    assert code != 0
    assert "SERIES_INSUFFICIENT_VALID_MATCHES" in capsys.readouterr().err
    argv_pass = _write_inputs(tmp_path, results, str(tmp_path / "out2")) + ["--require-valid", "2"]
    assert build_main(argv_pass) == 0


def test_count_valid_capture_results_counts_only_passed_valid_captures():
    docs = [_result_doc(1, valid=True), _result_doc(2, valid=False)]
    docs.append({"result": "passed", "outcome": "VALID_NO_CAPTURE"})
    docs.append({"result": "failed", "outcome": "VALID_CAPTURE"})
    assert count_valid_capture_results(docs) == 1


# --- requirement 4: brief variety ------------------------------------------------


def test_identical_blue_brief_families_are_flagged_low_without_failing(tmp_path):
    shared_family = select_blue_briefs("one-family-only", count=1)[0]
    runner = _ScriptedRunner([MODEL_A, MODEL_B, None], blue_briefs=[shared_family])

    summary = execute_series(_series_args(tmp_path), runner=runner)

    assert summary["brief_variety"] == "low"
    written = json.loads((tmp_path / "ep-v8.series.json").read_text(encoding="utf-8"))
    assert written["brief_variety"] == "low"


def test_distinct_blue_brief_families_do_not_flag_low_variety():
    briefs = select_blue_briefs("varied-seed", count=3)
    payloads = [
        {**_result_doc(index + 1, valid=bool(brief)), "blue_brief": brief_manifest(brief)}
        for index, brief in enumerate(briefs)
    ]

    summary = build_series_summary("ep-x", payloads)

    assert "brief_variety" not in summary


def test_missing_briefs_leave_variety_undetermined():
    summary = build_series_summary("ep-x", [_result_doc(1, valid=True) for _ in range(3)])
    assert "brief_variety" not in summary
