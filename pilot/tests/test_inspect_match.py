"""Tests for pilot.inspect_match (Inspect harness for one Command Code Match)."""

from __future__ import annotations

import pytest

from inspect_match import (
    COMPLETE_OUTCOMES,
    TASK_NAME,
    TASK_VERSION,
    _models,
    _namespace,
    sandboxer_match,
    score_payload,
)

IMAGE = "/var/lib/sandboxer/images/sandboxer-runner-v0.qcow2"
PROFILE = "/var/lib/sandboxer/profiles/sandboxer-runner.json"


def test_task_is_named_and_pinned_to_the_mock_runtime():
    match = sandboxer_match(match_id="calibration-test-001", image=IMAGE, profile=PROFILE)
    assert match.name == TASK_NAME
    assert match.version == TASK_VERSION
    assert str(match.model) == "mockllm/model"  # the task never calls generate


def test_task_exposes_one_sample_with_the_match_id():
    match = sandboxer_match(match_id="calibration-test-002", image=IMAGE, profile=PROFILE)
    samples = match.dataset
    assert len(samples) == 1
    assert samples[0].id == "calibration-test-002"


def test_task_metadata_declares_harness_and_calibration_only_publication():
    match = sandboxer_match(match_id="calibration-test-003", image=IMAGE, profile=PROFILE)
    assert match.metadata["harness"] == "inspect"
    assert match.metadata["provider"] == "command_code"
    assert match.metadata["publication_enabled"] is False
    assert match.metadata["is_calibration"] is True


def test_task_requires_match_id_image_and_profile():
    with pytest.raises(ValueError):
        sandboxer_match(match_id="", image=IMAGE, profile=PROFILE)
    with pytest.raises(ValueError):
        sandboxer_match(match_id="m", image="", profile=PROFILE)
    with pytest.raises(ValueError):
        sandboxer_match(match_id="m", image=IMAGE, profile="")


def test_task_rejects_malformed_model_pairs_before_provisioning():
    with pytest.raises(ValueError):
        sandboxer_match(match_id="m", image=IMAGE, profile=PROFILE, models="only-one")
    with pytest.raises(ValueError):
        sandboxer_match(match_id="m", image=IMAGE, profile=PROFILE, models="a/a,b/b,c/c")
    with pytest.raises(ValueError):
        sandboxer_match(match_id="m", image=IMAGE, profile=PROFILE, models="a/a,a/a")


def test_default_models_match_the_canonical_first_pair():
    assert _models("poolside/laguna-s-2.1-free,meta/muse-spark-1.2-contributor") == (
        "poolside/laguna-s-2.1-free",
        "meta/muse-spark-1.2-contributor",
    )


def test_models_accepts_inspect_style_split_lists():
    # ``inspect eval -T models=a,b`` may hand the task a pre-split list
    # instead of a single string; both forms must normalize identically.
    pair = ("deepseek/deepseek-v4-pro", "qwen/qwen3.7-flash")
    assert _models("deepseek/deepseek-v4-pro,qwen/qwen3.7-flash") == pair
    assert _models(list(pair)) == pair
    assert _models(tuple(pair)) == pair


def test_namespace_carries_engine_args_unchanged():
    ns = _namespace(
        match_id="m-1", image=IMAGE, profile=PROFILE,
        models="poolside/laguna-s-2.1-free,meta/muse-spark-1.2-contributor",
        seed="seed-1", runner_root="/var/lib/sandboxer/runners",
        evidence_dir="/var/lib/sandboxer/evidence", ttl_seconds=600,
        phase_timeout=180.0, blue_tokens=4096, red_tokens=4096,
        interview_tokens=1024, blue_turns=4, red_turns=5, blue_tools=8, red_tools=10,
    )
    assert ns.match_id == "m-1"
    assert ns.models == ("poolside/laguna-s-2.1-free", "meta/muse-spark-1.2-contributor")
    assert ns.seed == "seed-1"
    assert str(ns.image) == IMAGE
    assert str(ns.profile) == PROFILE
    assert ns.blue_tokens == 4096 and ns.red_tokens == 4096
    assert ns.blue_turns == 4 and ns.red_turns == 5
    assert ns.blue_tools == 8 and ns.red_tools == 10


def test_score_payload_accepts_complete_outcomes():
    for outcome in COMPLETE_OUTCOMES:
        payload = {
            "result": "passed",
            "outcome": outcome,
            "winner": "poolside/laguna-s-2.1-free",
            "captures": [True, False],
        }
        score = score_payload(payload)
        assert score.value == "C"
        assert outcome in score.explanation


def test_score_payload_rejects_incomplete_or_missing_payloads():
    assert score_payload(None).value == "I"
    assert score_payload("not-a-payload").value == "I"
    assert score_payload({}).value == "I"
    assert score_payload({"result": "passed", "outcome": "VALID_CAPTURE"}).value == "C"
    assert score_payload({"result": "failed", "outcome": "VALID_NO_CAPTURE"}).value == "I"
    assert score_payload({"result": "passed", "outcome": "NOT_AN_OUTCOME"}).value == "I"


def test_score_payload_metadata_is_redaction_safe():
    # The engine payload must never carry flags or provider credentials;
    # if one ever appears, it would end up in the Inspect eval log metadata.
    payload = {
        "result": "passed",
        "outcome": "VALID_CAPTURE",
        "winner": "meta/muse-spark-1.2-contributor",
        "captures": [False, True],
        "usage": {"blue": [{"final_text_sha256": "abc123"}]},
    }
    score = score_payload(payload)
    assert score.metadata == payload
    dumped = str(score.metadata).lower()
    assert "SANDBOXER-" not in dumped
    assert "api_key" not in dumped
    assert "token" not in dumped


def test_scorer_is_an_async_coroutine_and_wired_to_the_task():
    import inspect

    from inspect_match import match_outcome_score

    scorer = match_outcome_score()
    assert inspect.iscoroutinefunction(scorer)
    match = sandboxer_match(match_id="calibration-test-004", image=IMAGE, profile=PROFILE)
    assert len(match.scorer) == 1
    assert match.scorer[0].__name__ == "score"  # the wrapped outcome scorer
