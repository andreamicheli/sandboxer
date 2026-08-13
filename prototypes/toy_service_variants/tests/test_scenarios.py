import pytest

from prototypes.toy_service_variants.scenarios import (
    TRIGGERS,
    build_match_manifest,
    build_series_manifests,
)


def test_same_seed_is_reproducible() -> None:
    assert build_match_manifest(42) == build_match_manifest("42")


def test_both_sides_receive_the_same_scenario() -> None:
    manifest = build_match_manifest(137)
    sides = manifest["side_assignments"]
    assert sides["alpha"]["trigger_ref"] == "#/trigger"
    assert sides["beta"]["trigger_ref"] == "#/trigger"


def test_seed_sample_reaches_every_curated_family() -> None:
    observed = {
        build_match_manifest(seed)["trigger"]["family"]
        for seed in range(100)
    }
    expected = {trigger["family"] for trigger in TRIGGERS}
    assert observed == expected


def test_flag_is_not_derived_from_scenario_seed() -> None:
    manifest = build_match_manifest(901)
    serialized = repr(manifest["trigger"]).lower()
    assert "sandboxer-" not in serialized
    assert "flag_material" not in manifest["trigger"]


def test_catalog_is_read_only_and_synthetic() -> None:
    forbidden = {
        capability
        for trigger in TRIGGERS
        for capability in trigger.values()
        if isinstance(capability, str)
        and any(term in capability.lower() for term in ("subprocess", "shell execution", "real credential"))
    }
    assert not forbidden
    for trigger in TRIGGERS:
        assert "weakness" not in trigger
        assert "attack_evidence" not in trigger
        assert trigger["required_outcome"]
        assert trigger["open_design_choices"]


def test_best_of_three_uses_every_family_once() -> None:
    manifests = build_series_manifests("episode-1", matches=3)
    families = [manifest["trigger"]["family"] for manifest in manifests]
    assert len(families) == len(set(families)) == 3
    assert {manifest["series"]["match_index"] for manifest in manifests} == {1, 2, 3}


def test_series_is_reproducible() -> None:
    assert build_series_manifests(42) == build_series_manifests("42")


def test_series_rejects_catalog_overflow() -> None:
    with pytest.raises(ValueError, match="matches must be between"):
        build_series_manifests(42, matches=4)
