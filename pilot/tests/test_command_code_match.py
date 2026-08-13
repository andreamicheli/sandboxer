from sandboxer_v0.blue_briefs import select_blue_briefs
from scripts.run_command_code_match import _blue_prompt, _runner_name


def test_model_identity_maps_to_a_stable_runner_name():
    assert _runner_name("poolside/laguna-s-2.1-free") == "laguna-s-2-1-free"
    assert _runner_name("meta/muse-spark-1.2-contributor") == "muse-spark-1-2-contributor"


def test_blue_prompt_includes_a_shared_non_prescriptive_seed_brief():
    brief = select_blue_briefs("calibration-seed", count=1)[0]
    prompt = _blue_prompt(brief)
    assert brief.outcome in prompt
    assert brief.probe_description in prompt
    assert "does not prescribe an implementation or protected_policy" in prompt
