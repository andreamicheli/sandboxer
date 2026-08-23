"""Series robustness: budget retries, prompt allowlists, strict gate, variety.

Regression coverage for the episode-v8 postmortem (m2 died with
COMMAND_CODE_OUTPUT_BUDGET_EXCEEDED; m3 burned its turns calling the
non-whitelisted search_tools and never completed deploy_service):

  - the series driver retries exactly one ``*_BUDGET_EXCEEDED`` failure with
    1.5x token budgets under the derived identity ``<seed>-retry1`` and
    records which attempt produced the data point;
  - both phase prompts enumerate the EXACT ``PHASE_TOOLS`` allowlist and
    state that any other tool name will be rejected;
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
from sandboxer_v0.command_code import CommandCodeError
from sandboxer_v0.series_result import SERIES_SCHEMA, build_series_summary
import scripts.build_real_artifacts as build_module
from scripts.build_real_artifacts import count_valid_capture_results, main as build_main
from scripts.run_command_code_match import (
    MatchCalibrationError,
    PHASE_TOOLS,
    _blue_prompt,
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
