"""Tests for scripts/build_real_artifacts.py broadcast artifact inputs."""

from __future__ import annotations

import json

import pytest

from sandboxer_v0.artifact_converter import IDENTITY_A, IDENTITY_B

from scripts.build_real_artifacts import (
    DEFAULT_BENCHMARK_DATA,
    BenchmarkDataError,
    load_benchmark_snapshot,
)


def test_default_dataset_selects_the_five_dual_published_laguna_muse_rows():
    snapshot = load_benchmark_snapshot(DEFAULT_BENCHMARK_DATA, IDENTITY_A, IDENTITY_B)
    assert snapshot == {
        "Terminal-Bench 2.1": {IDENTITY_A: 0.702, IDENTITY_B: 0.829},
        "SWE-Bench Pro": {IDENTITY_A: 0.594, IDENTITY_B: 0.55},
        "DeepSWE 1.1": {IDENTITY_A: 0.404, IDENTITY_B: 0.593},
        "Toolathlon Verified": {IDENTITY_A: 0.497, IDENTITY_B: 0.494},
        "Arena Elo (Code)": {IDENTITY_A: 0.74, IDENTITY_B: 0.77},
    }


def test_missing_benchmark_data_file_raises_clean_error(tmp_path):
    with pytest.raises(BenchmarkDataError, match="benchmark data file not found"):
        load_benchmark_snapshot(tmp_path / "absent.json", IDENTITY_A, IDENTITY_B)


def test_pair_selection_matches_identity_names_in_both_orders_and_spellings(tmp_path):
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps({
        "schema": "sandboxer.benchmark-snapshot.v1",
        "pairs": {
            # slug-style key, as in docs/benchmark-dataset-series001.md examples
            "laguna-s-2.1 vs tencent-hy3": {
                "benchmarks": [{"benchmark": "Terminal-Bench",
                                "values": {"Laguna S 2.1": 0.702, "Tencent Hy3": 0.644}}],
            },
            # display-name key, reversed order relative to the query below
            "Step 3.7 Flash vs DeepSeek V4 Pro": {
                "benchmarks": [{"benchmark": "SWE-Bench Pro",
                                "values": {"Step 3.7 Flash": 0.563, "DeepSeek V4 Pro": 0.554}}],
            },
        },
    }), encoding="utf-8")
    assert load_benchmark_snapshot(path, "Laguna S 2.1", "Tencent Hy3") == {
        "Terminal-Bench": {"Laguna S 2.1": 0.702, "Tencent Hy3": 0.644},
    }
    assert load_benchmark_snapshot(path, "DeepSeek V4 Pro", "Step 3.7 Flash") == {
        "SWE-Bench Pro": {"DeepSeek V4 Pro": 0.554, "Step 3.7 Flash": 0.563},
    }


def test_unknown_pair_falls_back_to_empty_snapshot_without_crashing(tmp_path):
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps({
        "schema": "sandboxer.benchmark-snapshot.v1",
        "pairs": {"Alpha vs Beta": {"benchmarks": [
            {"benchmark": "Terminal-Bench 2.1", "values": {"Alpha": 0.5, "Beta": 0.4}}]}},
    }), encoding="utf-8")
    assert load_benchmark_snapshot(path, IDENTITY_A, IDENTITY_B) == {}


def test_shipped_dataset_pairs_are_well_formed_and_dual_published():
    data = json.loads(DEFAULT_BENCHMARK_DATA.read_text(encoding="utf-8"))
    assert data["schema"] == "sandboxer.benchmark-snapshot.v1"
    assert len(data["pairs"]) >= 15
    for key, section in data["pairs"].items():
        left, right = (part.strip() for part in key.split(" vs "))
        assert section["benchmarks"], f"empty pair section: {key}"
        for row in section["benchmarks"]:
            assert row["benchmark"], key
            assert set(row["values"]) == {left, right}, row
            assert all(isinstance(v, (int, float)) and not isinstance(v, bool)
                       and 0 <= v <= 1 for v in row["values"].values()), row
