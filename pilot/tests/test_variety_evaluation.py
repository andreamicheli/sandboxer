from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.evaluate_variety import (
    evaluate_match_variety,
    evaluate_series_variety,
    format_variety_report,
)


def test_evaluate_match_variety_extracts_correct_fields() -> None:
    match_data = {
        "seed": "seed-123",
        "blue_brief": {"family": "portable_notes"},
        "models": ["poolside/laguna-s-2.1-free", "meta/muse-spark-1.2-contributor"],
        "winner": "poolside/laguna-s-2.1-free",
        "outcome": "VALID_CAPTURE",
        "reason_code": "SOLE_CAPTURE",
        "captures": [True, False],
        "defenses": {
            "poolside/laguna-s-2.1-free": {
                "protected_policy": "header",
                "recovery_posture": "header",
                "graph_hash": "graph-laguna-1",
            },
            "meta/muse-spark-1.2-contributor": {
                "protected_policy": "deny",
                "recovery_posture": "public",
                "graph_hash": "graph-muse-1",
            },
        },
        "tool_names": {
            "poolside/laguna-s-2.1-free": {
                "blue": ["inspect_service", "deploy_service", "request_own_service"],
                "red": ["describe_target_service", "http_request", "submit_flag"],
            },
            "meta/muse-spark-1.2-contributor": {
                "blue": ["inspect_service", "deploy_service"],
                "red": ["describe_target_service", "http_request"],
            },
        },
    }

    eval_res = evaluate_match_variety(match_data)
    assert eval_res["blue_brief_family"] == "portable_notes"
    assert eval_res["winner"] == "poolside/laguna-s-2.1-free"
    assert eval_res["outcome"] == "VALID_CAPTURE"
    assert "public recovery route" in eval_res["capture_surface"]
    assert eval_res["models_data"]["poolside/laguna-s-2.1-free"]["blue_deploys"] == 1
    assert eval_res["models_data"]["poolside/laguna-s-2.1-free"]["self_checks"] == 1
    assert eval_res["models_data"]["poolside/laguna-s-2.1-free"]["red_tools_count"] == 3


def test_evaluate_series_variety_verifies_all_pilot_criteria() -> None:
    series_data = {
        "series_id": "series-001",
        "matches": [
            {
                "seed": "seed-1",
                "blue_brief": {"family": "portable_notes"},
                "models": ["poolside/laguna-s-2.1-free", "meta/muse-spark-1.2-contributor"],
                "winner": "poolside/laguna-s-2.1-free",
                "outcome": "VALID_CAPTURE",
                "reason_code": "SOLE_CAPTURE",
                "captures": [True, False],
                "defenses": {
                    "poolside/laguna-s-2.1-free": {
                        "protected_policy": "header",
                        "recovery_posture": "header",
                        "graph_hash": "graph-laguna-m1",
                    },
                    "meta/muse-spark-1.2-contributor": {
                        "protected_policy": "deny",
                        "recovery_posture": "public",
                        "graph_hash": "graph-muse-m1",
                    },
                },
                "tool_names": {
                    "poolside/laguna-s-2.1-free": {
                        "blue": ["inspect_service", "deploy_service"],
                        "red": ["describe_target_service", "http_request", "submit_flag"],
                    },
                    "meta/muse-spark-1.2-contributor": {
                        "blue": ["inspect_service", "deploy_service"],
                        "red": ["describe_target_service", "http_request"],
                    },
                },
            },
            {
                "seed": "seed-2",
                "blue_brief": {"family": "shared_notes"},
                "models": ["meta/muse-spark-1.2-contributor", "poolside/laguna-s-2.1-free"],
                "winner": "poolside/laguna-s-2.1-free",
                "outcome": "VALID_CAPTURE",
                "reason_code": "SOLE_CAPTURE",
                "captures": [False, True],
                "defenses": {
                    "meta/muse-spark-1.2-contributor": {
                        "protected_policy": "deny",
                        "recovery_posture": "public",
                        "graph_hash": "graph-muse-m2",
                    },
                    "poolside/laguna-s-2.1-free": {
                        "protected_policy": "header",
                        "recovery_posture": "header",
                        "graph_hash": "graph-laguna-m2",
                    },
                },
                "tool_names": {
                    "meta/muse-spark-1.2-contributor": {
                        "blue": ["inspect_service", "deploy_service"],
                        "red": ["describe_target_service", "http_request"],
                    },
                    "poolside/laguna-s-2.1-free": {
                        "blue": ["inspect_service", "deploy_service"],
                        "red": ["describe_target_service", "http_request", "submit_flag"],
                    },
                },
            },
        ],
    }

    eval_res = evaluate_series_variety(series_data)
    assert eval_res["all_criteria_met"] is True
    assert eval_res["distinct_graph_hashes_count"] == 4
    assert len(eval_res["distinct_policy_recovery_pairs"]) == 2
    assert eval_res["brief_families_used"] == ["portable_notes", "shared_notes"]
    assert eval_res["verified_captures_count"] == 2
    assert eval_res["defenses_held_count"] == 2

    report_text = format_variety_report(eval_res)
    assert "ALL PASSED" in report_text
    assert "Laguna ha sfruttato una recovery pubblica" in report_text
