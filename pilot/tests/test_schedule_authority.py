"""Structural guarantees of the schedule authority (schedule.validate_and_pack).

The scheduler — not the model prompt and not ad-hoc caller patches — owns the
editorial timeline: budgets are clamped, offsets are suggestions, lines never
overlap, never leave their scene window, and a fully non-conforming draft can
only ever yield the deterministic fallback.
"""

from __future__ import annotations

from sandboxer_v0.commentary import fallback_intro_commentary
from sandboxer_v0.schedule import (
    PROVENANCE_FALLBACK,
    PROVENANCE_MODEL_DRAFT,
    LineBudget,
    validate_and_pack,
)
from sandboxer_v0.video import build_video_manifest


def _line(**overrides):
    line = {
        "scene": "cold_open",
        "offset_seconds": 1.0,
        "text": "short line",
        "voice_role": "play_by_play",
        "line_type": "editorial",
    }
    line.update(overrides)
    return line


def _budget(**overrides):
    budget = {
        "max_lines": 4,
        "max_words_per_line": 6,
        "max_total_words": 24,
        "window_start_s": 0.0,
        "window_end_s": 8.0,
    }
    budget.update(overrides)
    return LineBudget(**budget)


def test_oversized_draft_is_clamped_to_the_line_budget():
    draft = [_line(offset_seconds=float(i), text=f"line {i}") for i in range(9)]
    result = validate_and_pack(draft, _budget(max_lines=4))
    assert len(result.blocks) == 4
    assert result.dropped == 5
    assert sum(reason.endswith("over_line_budget") for reason in result.drop_reasons) == 5
    assert all(block["provenance"] == PROVENANCE_MODEL_DRAFT for block in result.blocks)


def test_offsets_outside_the_window_are_clamped_and_marked_repaired():
    draft = [
        _line(offset_seconds=-12.0),
        _line(offset_seconds=99.0, text="another line"),
    ]
    result = validate_and_pack(draft, _budget(window_start_s=0.0, window_end_s=8.0))
    assert [block["offset_seconds"] for block in result.blocks] == [
        min(max(-12.0, 0.0), 8.0),
        result.blocks[0]["end_seconds"],
    ]
    assert result.blocks[0]["offset_seconds"] == 0.0
    assert result.repaired == 2
    assert all(block["provenance"] == PROVENANCE_FALLBACK for block in result.blocks)


def test_overlapping_lines_are_repacked_back_to_back_without_overlap():
    # All three lines claim the same moment; the scheduler must repack them.
    # Three 1.5s-minimum blocks plus gaps need more than the default 8s window,
    # so this budget widens it — the point here is ordering, not scarcity.
    draft = [_line(offset_seconds=3.0, text=f"overlap {i}") for i in range(3)]
    result = validate_and_pack(draft, _budget(window_end_s=12.0), gap_s=0.5)
    first, second, third = result.blocks
    assert second["offset_seconds"] >= first["end_seconds"]
    assert third["offset_seconds"] >= second["end_seconds"]
    assert third["offset_seconds"] - second["end_seconds"] == 0.5
    assert result.dropped == 0


def test_all_nonconforming_draft_yields_full_deterministic_fallback():
    draft = [
        _line(text=""),
        _line(text=" ".join(["word"] * 30)),
        _line(scene="mystery_scene"),
        _line(offset_seconds="whenever"),
    ]
    budgets = {"cold_open": _budget()}
    fallback = [_line(offset_seconds=2.0, text="deterministic greeting")]
    result = validate_and_pack(draft, budgets, fallback=fallback)
    assert result.used_fallback is True
    assert [block["text"] for block in result.blocks] == ["deterministic greeting"]
    assert all(block["provenance"] == PROVENANCE_FALLBACK for block in result.blocks)
    assert result.dropped == len(draft)  # every draft line rejected, fallback clean
    # An empty draft stays empty: silence is a legitimate schedule.
    empty = validate_and_pack([], budgets, fallback=fallback)
    assert empty.blocks == [] and empty.used_fallback is False


def test_intro_lines_are_never_scheduled_after_window_end():
    intro_budgets = {
        "cold_open": LineBudget(max_lines=3, max_words_per_line=20, max_total_words=60,
                                window_start_s=0.0, window_end_s=7.25),
        "model_cards_and_rules": LineBudget(max_lines=5, max_words_per_line=20, max_total_words=100,
                                            window_start_s=0.0, window_end_s=19.25),
    }
    draft = [
        _line(scene="cold_open", offset_seconds=0.5, text="welcome to the show"),
        _line(scene="cold_open", offset_seconds=999.0, text="clamped into the window"),
        # Fits neither by clamp nor repack: 18.9 + duration > 19.25, so the
        # authority must drop it rather than schedule it past the scene cut.
        _line(scene="model_cards_and_rules", offset_seconds=18.9, text="late cards line"),
    ]
    result = validate_and_pack(draft, intro_budgets, gap_s=0.4)
    assert result.blocks
    for block in result.blocks:
        budget = intro_budgets[block["scene"]]
        assert budget.window_start_s <= block["offset_seconds"] <= budget.window_end_s
        assert block["end_seconds"] <= budget.window_end_s
    assert [block["text"] for block in result.blocks] == ["welcome to the show",
                                                          "clamped into the window"]
    clamped = next(block for block in result.blocks if block["text"] == "clamped into the window")
    assert clamped["provenance"] == PROVENANCE_FALLBACK
    assert result.repaired >= 1
    assert "2:window_overflow" in result.drop_reasons


def test_video_manifest_packs_malformed_commentary_into_a_valid_schedule():
    replay = {"schema_version": "sandboxer.replay.v1", "source_bundle_hash": "a" * 64,
              "panes": [{"identity": "DeepSeek V4 Pro"}, {"identity": "MiMo V2.5 Pro"}],
              "frames": [
                  {"sequence": 1, "at_monotonic_ns": 0, "event_id": "e1", "event_type": "MATCH_STARTED",
                   "phase": "blue", "pane": 0, "text": "defend"},
                  {"sequence": 2, "at_monotonic_ns": 2_000_000_000, "event_id": "e2", "event_type": "MODEL_RESPONSE",
                   "phase": "blue", "pane": 1, "text": "inspect"}]}
    malformed = [
        {"voice_role": "play_by_play", "line_type": "observed", "event_ids": ["ghost"], "text": "ungrounded"},
        {"voice_role": "play_by_play", "line_type": "observed", "event_ids": ["e2"],
         "text": " ".join(["overbudget"] * 40)},
        {"voice_role": "play_by_play", "line_type": "observed", "event_ids": ["e1"],
         "offset_seconds": -500.0, "text": "It seems anchored deep before the match."},
    ]
    manifest = build_video_manifest(replay, report={"report_url": "r", "outcome": {}},
                                    model_metadata={}, benchmark_snapshot={}, commentary=malformed)
    match_start = manifest["scenes"][0]["duration_frames"] + manifest["scenes"][1]["duration_frames"]
    lines = manifest["commentary"]
    assert lines
    for left, right in zip(lines, lines[1:]):
        assert left["end_frame"] <= right["start_frame"]
    assert lines[-1]["end_frame"] < match_start + manifest["scenes"][2]["duration_frames"]
    assert any(line["provenance"] == PROVENANCE_FALLBACK for line in lines)


def test_video_manifest_never_lets_intro_lines_bleed_into_match_scenes():
    replay = {"schema_version": "sandboxer.replay.v1", "source_bundle_hash": "a" * 64,
              "panes": [{"identity": "DeepSeek V4 Pro"}, {"identity": "MiMo V2.5 Pro"}],
              "frames": [
                  {"sequence": 1, "at_monotonic_ns": 0, "event_id": "e1", "event_type": "MATCH_STARTED",
                   "phase": "blue", "pane": 0, "text": "defend"}]}
    intro = [
        {"voice_role": "play_by_play", "line_type": "editorial", "scene": "model_cards_and_rules",
         "offset_seconds": 12.0, "text": "an offset that pretends to be absolute"},
        {"voice_role": "analyst", "line_type": "editorial", "scene": "model_cards_and_rules",
         "offset_seconds": 16.0, "text": "another absolute-looking offset"},
    ]
    manifest = build_video_manifest(replay, report={"report_url": "r", "outcome": {}},
                                    model_metadata={}, benchmark_snapshot={}, intro_commentary=intro)
    match_start = manifest["scenes"][0]["duration_frames"] + manifest["scenes"][1]["duration_frames"]
    intro_lines = [line for line in manifest["commentary"] if not line["event_ids"]]
    assert len(intro_lines) == 2
    assert all(line["end_frame"] < match_start for line in intro_lines)
