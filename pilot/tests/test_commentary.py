from __future__ import annotations

import pytest

from sandboxer_v0.commentary import (
    CommentaryError,
    GeminiCommentaryDrafter,
    validate_commentary,
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


class _Response:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeModels:
    def __init__(self, text: str) -> None:
        self._text = text
        self.last_call: tuple | None = None

    def generate_content(self, *, model: str, contents: str):
        self.last_call = (model, contents)
        return _Response(self._text)


class _FakeClient:
    def __init__(self, text: str) -> None:
        self.models = _FakeModels(text)


def _replay():
    return {
        "panes": [{"identity": "DeepSeek V4 Pro"}, {"identity": "MiMo V2.5 Pro"}],
        "frames": _FRAMES,
    }


def test_gemini_drafter_drafts_and_validates_lines():
    client = _FakeClient(
        '{"lines":[{"voice_role":"analyst","line_type":"interpreted",'
        '"event_ids":["e2"],"text":"It seems MiMo attacks."}]}'
    )
    drafter = GeminiCommentaryDrafter(client=client)
    lines = drafter.draft(_replay(), {})
    assert lines[0]["text"].startswith("It seems")
    assert lines[0]["voice_role"] == "analyst"


def test_gemini_drafter_rejects_ungrounded_output():
    client = _FakeClient(
        '{"lines":[{"voice_role":"play_by_play","line_type":"observed",'
        '"event_ids":["ghost"],"text":"hi"}]}'
    )
    with pytest.raises(CommentaryError, match="COMMENTARY_INVALID"):
        GeminiCommentaryDrafter(client=client).draft(_replay(), {})


def test_gemini_drafter_rejects_malformed_json():
    with pytest.raises(CommentaryError, match="COMMENTARY_PARSE_FAILED"):
        GeminiCommentaryDrafter(client=_FakeClient("not json")).draft(_replay(), {})
