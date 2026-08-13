from scripts.run_command_code_match import _runner_name


def test_model_identity_maps_to_a_stable_runner_name():
    assert _runner_name("poolside/laguna-s-2.1-free") == "laguna-s-2-1-free"
    assert _runner_name("meta/muse-spark-1.2-contributor") == "muse-spark-1-2-contributor"
