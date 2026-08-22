from __future__ import annotations

import pytest

from sandboxer_v0.commentary import (
    CommentaryError,
    HeadlessCommentaryDrafter,
    HeadlessIntroCommentaryDrafter,
    draft_intro_commentary,
    validate_commentary,
    validate_intro_commentary,
)

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
        "scene": "cold_open",
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
        {"voice_role": "play_by_play", "line_type": "editorial", "scene": "cold_open", "offset_seconds": 4.0, "text": "Welcome."},
        {"voice_role": "analyst", "line_type": "interpreted", "scene": "model_cards_and_rules", "offset_seconds": 1.0, "text": "It seems Muse moves fast.", "model": "Muse Spark 1.2"},
    ]
    adapter = _FakeAdapter("```json\n" + __import__("json").dumps(payload) + "\n```")
    lines = HeadlessIntroCommentaryDrafter(adapter=adapter).draft(["Laguna S 2.1", "Muse Spark 1.2"], {})
    assert [line["scene"] for line in lines] == ["cold_open", "model_cards_and_rules"]
    assert lines[1]["model"] == "Muse Spark 1.2"


def test_draft_intro_commentary_raises_on_invalid():
    with pytest.raises(CommentaryError, match="INTRO_COMMENTARY_INVALID"):
        draft_intro_commentary(["Laguna S 2.1"], {}, drafter=HeadlessIntroCommentaryDrafter(adapter=_FakeAdapter('{"lines":[{"voice_role":"x"}]}')))

