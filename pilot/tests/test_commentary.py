from __future__ import annotations

import json

import pytest

from sandboxer_v0.commentary import (
    PROVENANCE_DETERMINISTIC,
    CommentaryError,
    HeadlessCommentaryDrafter,
    HeadlessIntroCommentaryDrafter,
    build_commentary,
    draft_intro_commentary,
    validate_commentary,
    validate_intro_commentary,
)
from sandboxer_v0.video import VideoError, build_video_manifest

_FRAMES = [
    {"event_id": "e1", "phase": "blue", "pane": 0, "text": "defend"},
    {"event_id": "e2", "phase": "red", "pane": 1, "text": "attack"},
]


def _line(**overrides):
    line = {
        "voice_role": "play_by_play",
        "line_type": "observed",
        "event_ids": ["e1"],
        "text": "DeepSeek defends.",
    }
    line.update(overrides)
    return line


def test_validate_commentary_accepts_grounded_typed_lines():
    assert validate_commentary([_line()], _FRAMES) == ()


def test_validate_commentary_flags_unknown_event_ids():
    failures = validate_commentary([_line(event_ids=["ghost"])], _FRAMES)
    assert any("unknown event_ids" in failure for failure in failures)


def test_validate_commentary_requires_hedge_marker_on_interpreted_lines():
    assert validate_commentary(
        [_line(line_type="interpreted", text="DeepSeek will lose.")], _FRAMES
    )
    assert validate_commentary(
        [_line(line_type="interpreted", text="It seems DeepSeek will lose.")], _FRAMES
    ) == ()


def test_validate_commentary_flags_bad_role_and_type():
    failures = validate_commentary([_line(voice_role="color")], _FRAMES)
    assert any("voice_role" in failure for failure in failures)
    failures = validate_commentary([_line(line_type="opinion")], _FRAMES)
    assert any("line_type" in failure for failure in failures)


class _FakeAdapter:
    def __init__(self, text: str) -> None:
        self._text = text
        self.last_prompt: str | None = None

    def complete(self, prompt: str) -> str:
        self.last_prompt = prompt
        return self._text


def _replay():
    return {
        "panes": [{"identity": "DeepSeek V4 Pro"}, {"identity": "MiMo V2.5 Pro"}],
        "frames": _FRAMES,
    }


def test_headless_drafter_drafts_and_validates_lines():
    adapter = _FakeAdapter(
        '{"lines":[{"voice_role":"analyst","line_type":"interpreted",'
        '"event_ids":["e2"],"text":"It seems MiMo attacks."}]}'
    )
    drafter = HeadlessCommentaryDrafter(adapter=adapter)
    lines = drafter.draft(_replay(), {})
    assert lines[0]["text"].startswith("It seems")
    assert lines[0]["voice_role"] == "analyst"
    assert adapter.last_prompt  # the adapter was driven by the drafted prompt


def test_headless_drafter_rejects_ungrounded_output():
    adapter = _FakeAdapter(
        '{"lines":[{"voice_role":"play_by_play","line_type":"observed",'
        '"event_ids":["ghost"],"text":"hi"}]}'
    )
    with pytest.raises(CommentaryError, match="COMMENTARY_INVALID"):
        HeadlessCommentaryDrafter(adapter=adapter).draft(_replay(), {})


def test_headless_drafter_rejects_malformed_json():
    with pytest.raises(CommentaryError, match="COMMENTARY_PARSE_FAILED"):
        HeadlessCommentaryDrafter(adapter=_FakeAdapter("not json")).draft(_replay(), {})


def test_headless_drafter_downgrades_unhedged_interpreted_line():
    adapter = _FakeAdapter(
        '{"lines":[{"voice_role":"analyst","line_type":"interpreted",'
        '"event_ids":["e2"],"text":"MiMo attacks the left flank."}]}'
    )
    lines = HeadlessCommentaryDrafter(adapter=adapter).draft(_replay(), {})
    assert lines[0]["line_type"] == "editorial"


def test_intro_drafter_downgrades_unhedged_interpreted_line():
    payload = [
        {"voice_role": "analyst", "line_type": "interpreted", "scene": "model_cards_and_rules",
         "offset_seconds": 1.0, "text": "Muse moves fast.", "model": "Muse Spark 1.2"},
    ]
    adapter = _FakeAdapter(__import__("json").dumps(payload))
    lines = HeadlessIntroCommentaryDrafter(adapter=adapter).draft(
        ["Laguna S 2.1", "Muse Spark 1.2"], {}
    )
    assert lines[0]["line_type"] == "editorial"


def _intro_line(**overrides):
    line = {
        "voice_role": "play_by_play",
        "line_type": "editorial",
        "scene": "model_cards_and_rules",
        "offset_seconds": 3.0,
        "text": "Welcome back to Sandboxer.",
    }
    line.update(overrides)
    return line


def test_validate_intro_commentary_accepts_valid_lines():
    assert validate_intro_commentary([_intro_line()], identities=["Laguna S 2.1", "Muse Spark 1.2"]) == ()


def test_validate_intro_commentary_rejects_bad_scene_and_hedge():
    failures = validate_intro_commentary([_intro_line(scene="recap")], identities=["Laguna S 2.1"])
    assert any("scene" in f for f in failures)
    failures = validate_intro_commentary(
        [_intro_line(line_type="interpreted", text="Muse will lose.")], identities=["Laguna S 2.1"]
    )
    assert any("hedge" in f for f in failures)


def test_intro_drafter_parses_fenced_json_list():
    payload = [
        {"voice_role": "play_by_play", "line_type": "editorial", "scene": "model_cards_and_rules", "offset_seconds": 4.0, "text": "Welcome."},
        {"voice_role": "analyst", "line_type": "interpreted", "scene": "model_cards_and_rules", "offset_seconds": 1.0, "text": "It seems Muse moves fast.", "model": "Muse Spark 1.2"},
    ]
    adapter = _FakeAdapter("```json\n" + __import__("json").dumps(payload) + "\n```")
    lines = HeadlessIntroCommentaryDrafter(adapter=adapter).draft(["Laguna S 2.1", "Muse Spark 1.2"], {})
    assert [line["scene"] for line in lines] == ["model_cards_and_rules", "model_cards_and_rules"]
    assert lines[1]["model"] == "Muse Spark 1.2"


def test_draft_intro_commentary_raises_on_invalid():
    with pytest.raises(CommentaryError, match="INTRO_COMMENTARY_INVALID"):
        draft_intro_commentary(["Laguna S 2.1"], {}, drafter=HeadlessIntroCommentaryDrafter(adapter=_FakeAdapter('{"lines":[{"voice_role":"x"}]}')))


# --- Deterministic dense play-by-play (build_commentary) ---------------------

FPS = 30
_IDENTITIES = ["Laguna S 2.1", "Muse Spark 1.2"]
_SCHEMA_KEYS = {"voice_role", "model", "start_frame", "end_frame", "text",
                "event_ids", "line_type", "provenance"}
_SECOND = 1_000_000_000


def _frame(seq, ns, eid, etype, phase, pane, text):
    return {"sequence": seq, "at_monotonic_ns": int(ns), "event_id": eid,
            "event_type": etype, "phase": phase, "pane": pane, "text": text,
            "match_number": 1}


def _fixture_frames():
    """A converter-shaped 26-event match: blue build-up, dead air, red race."""
    return [
        _frame(1, 0, "e01", "MATCH_STARTED", "blue", 0, "match started - two isolated services, one flag each"),
        _frame(2, 1, "e02", "MATCH_STARTED", "blue", 1, "match started - services coming up"),
        _frame(3, 3 * _SECOND, "e03", "TOOL_CALL", "blue", 0, "inspect: declared service surface"),
        _frame(4, 8 * _SECOND, "e04", "TOOL_CALL", "blue", 0, "deploy: proposed service spec"),
        _frame(5, 12 * _SECOND, "e05", "MODEL_RESPONSE", "blue", 0, "defense promoted: STRICT policy, RESTART recovery"),
        _frame(6, 20 * _SECOND, "e06", "TOOL_CALL", "blue", 1, "inspect: declared service surface"),
        _frame(7, 22 * _SECOND, "e07", "TOOL_CALL", "blue", 1, "inspect: declared service surface"),
        _frame(8, 26 * _SECOND, "e08", "TOOL_CALL", "blue", 1, "deploy: proposed service spec"),
        # 24 seconds of dead air before the second defense is promoted.
        _frame(9, 50 * _SECOND, "e09", "MODEL_RESPONSE", "blue", 1, "defense promoted: ALLOW policy, RETRY recovery"),
        _frame(10, 52 * _SECOND, "e10", "PHASE_TRANSITION", "blue", 0, "blue phase complete - defenses are live"),
        _frame(11, 56 * _SECOND, "e11", "TOOL_CALL", "red", 1, "target: opponent service contract"),
        _frame(12, 58 * _SECOND, "e12", "TOOL_CALL", "red", 1, "probe: HTTP request on target"),
        _frame(13, 60 * _SECOND, "e13", "TOOL_CALL", "red", 1, "probe: HTTP request on target"),
        _frame(14, 63 * _SECOND, "e14", "TOOL_CALL", "red", 1, "verify: objective token submission"),
        _frame(15, 65 * _SECOND, "e15", "TOOL_CALL", "red", 1, "probe: HTTP request on target"),
        _frame(16, 67 * _SECOND, "e16", "TOOL_CALL", "red", 1, "verify: objective token submission"),
        _frame(17, 69 * _SECOND, "e17", "TOOL_CALL", "red", 0, "verify: self-service health check"),
        _frame(18, 71 * _SECOND, "e18", "SUBMISSION_VERIFIED", "red", 1, "objective submission verified"),
        _frame(19, 73 * _SECOND, "e19", "TOOL_CALL", "red", 0, "target: opponent service contract"),
        _frame(20, 75 * _SECOND, "e20", "TOOL_CALL", "red", 0, "probe: HTTP request on target"),
        _frame(21, 77 * _SECOND, "e21", "TOOL_CALL", "red", 0, "verify: objective token submission"),
        _frame(22, 79 * _SECOND, "e22", "TOOL_CALL", "red", 0, "probe: HTTP request on target"),
        _frame(23, 81 * _SECOND, "e23", "TOOL_CALL", "red", 0, "verify: objective token submission"),
        _frame(24, 83 * _SECOND, "e24", "TOOL_CALL", "red", 0, "verify: objective token submission"),
        _frame(25, 86 * _SECOND, "e25", "MATCH_FINISHED", "red", 0, "match finished - FLAG_CAPTURED, winner Laguna S 2.1"),
        _frame(26, 88 * _SECOND, "e26", "MATCH_FINISHED", "red", 1, "teardown complete - runners destroyed"),
    ]


def test_build_commentary_empty_input_yields_empty_list():
    assert build_commentary([], _IDENTITIES, FPS) == []


def test_build_commentary_covers_every_action_event():
    lines = build_commentary(_fixture_frames(), _IDENTITIES, FPS)
    covered = {eid for line in lines for eid in line["event_ids"]}
    missing = {frame["event_id"] for frame in _fixture_frames()} - covered
    assert not missing, f"uncovered action events: {sorted(missing)}"


def test_build_commentary_lines_match_manifest_schema():
    lines = build_commentary(_fixture_frames(), _IDENTITIES, FPS)
    assert lines
    for line in lines:
        assert set(line) == _SCHEMA_KEYS
        assert line["provenance"] == PROVENANCE_DETERMINISTIC
        assert line["voice_role"] in {"play_by_play", "analyst"}
        assert line["line_type"] in {"observed", "interpreted", "editorial"}
        assert line["text"].strip()
        assert line["event_ids"]
        assert line["end_frame"] > line["start_frame"]


def test_build_commentary_windows_never_overlap():
    lines = sorted(build_commentary(_fixture_frames(), _IDENTITIES, FPS),
                   key=lambda item: item["start_frame"])
    for left, right in zip(lines, lines[1:]):
        assert left["end_frame"] <= right["start_frame"]


def test_build_commentary_play_by_play_aligns_to_event_frame():
    lines = build_commentary(_fixture_frames(), _IDENTITIES, FPS)
    inspect = next(line for line in lines if line["event_ids"] == ["e03"])
    assert inspect["voice_role"] == "play_by_play"
    assert inspect["start_frame"] == 3 * FPS
    assert inspect["model"] == "Laguna S 2.1"
    promote = next(line for line in lines if line["event_ids"] == ["e05"])
    assert "STRICT policy" in promote["text"] and "RESTART recovery" in promote["text"]


def test_build_commentary_merges_same_frame_match_start_pair():
    lines = build_commentary(_fixture_frames(), _IDENTITIES, FPS)
    opener = next(line for line in lines if "e01" in line["event_ids"])
    assert "e02" in opener["event_ids"]
    assert opener["start_frame"] == 0


def test_build_commentary_fills_dead_air_with_seen_action_recap():
    lines = build_commentary(_fixture_frames(), _IDENTITIES, FPS)
    fillers = [line for line in lines
               if line["voice_role"] == "analyst"
               and 26 * FPS < line["start_frame"] < 50 * FPS]
    assert fillers, "the 24s dead-air stretch was not filled"
    seen_before_gap = {f"e{index:02d}" for index in range(1, 9)}
    assert all(set(line["event_ids"]) <= seen_before_gap for line in fillers)


def test_build_commentary_flags_lopsided_defense_promotion():
    lines = build_commentary(_fixture_frames(), _IDENTITIES, FPS)
    notes = [line for line in lines
             if line["voice_role"] == "analyst" and "Telemetry note:" in line["text"]]
    assert len(notes) == 1
    note = notes[0]
    assert note["line_type"] == "observed"
    assert set(note["event_ids"]) == {"e05", "e09"}
    # Elapsed times come straight from the fixture timestamps: 12s vs 50s.
    assert "Laguna S 2.1" in note["text"] and "Muse Spark 1.2" in note["text"]
    assert " 12s" in note["text"] and " 50s" in note["text"]


def test_build_commentary_repeated_offense_gets_pattern_note():
    lines = build_commentary(_fixture_frames(), _IDENTITIES, FPS)
    notes = [line for line in lines if "in a row from" in line["text"]]
    assert notes
    probe_ids = {f"e{index:02d}" for index in range(11, 17)} | {
        f"e{index:02d}" for index in range(19, 25)}
    for note in notes:
        assert note["voice_role"] == "analyst"
        assert set(note["event_ids"]) <= probe_ids


def test_build_commentary_is_deterministic():
    first = build_commentary(_fixture_frames(), _IDENTITIES, FPS)
    second = build_commentary(_fixture_frames(), _IDENTITIES, FPS)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_video_manifest_schedules_dense_commentary_end_to_end():
    replay = {"schema_version": "sandboxer.replay.v1", "source_bundle_hash": "a" * 64,
              "panes": [{"identity": "Laguna S 2.1"}, {"identity": "Muse Spark 1.2"}],
              "layout": {"split": {"left": .5, "right": .5, "permanent": True}},
              "frames": _fixture_frames()}
    dense = build_commentary(replay["frames"], _IDENTITIES, FPS)
    manifest = build_video_manifest(replay, report={"report_url": "r", "outcome": {}},
                                    model_metadata={}, benchmark_snapshot={},
                                    dense_commentary=dense)
    lines = manifest["commentary"]
    texts = {line["text"] for line in lines}
    dense_lines = [line for line in lines if line["provenance"] == PROVENANCE_DETERMINISTIC]
    assert len(dense_lines) == len(dense)
    assert all(line["text"] in texts for line in dense)
    assert all(left["end_frame"] <= right["start_frame"]
               for left, right in zip(lines, lines[1:]))
    covered = {eid for line in dense_lines for eid in line["event_ids"]}
    assert {frame["event_id"] for frame in _fixture_frames()} <= covered


def test_video_manifest_rejects_ungrounded_dense_commentary():
    replay = {"schema_version": "sandboxer.replay.v1", "source_bundle_hash": "a" * 64,
              "panes": [{"identity": "Laguna S 2.1"}, {"identity": "Muse Spark 1.2"}],
              "layout": {"split": {"left": .5, "right": .5, "permanent": True}},
              "frames": _fixture_frames()}
    ghost = [{"voice_role": "play_by_play", "model": "", "start_frame": 0,
              "end_frame": 30, "text": "hi", "event_ids": ["ghost"],
              "line_type": "observed", "provenance": PROVENANCE_DETERMINISTIC}]
    with pytest.raises(VideoError, match="VIDEO_DENSE_COMMENTARY_INVALID"):
        build_video_manifest(replay, report={"report_url": "r", "outcome": {}},
                             model_metadata={}, benchmark_snapshot={},
                             dense_commentary=ghost)

