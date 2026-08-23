"""Emotion/prosody annotation for Fish Audio TTS commentary.

Contract under test (``sandboxer_v0.commentary_emotion``):

- capture beats get excitement, red-phase actions tension, blue-phase
  defenses (and analyst color) steady analysis, interview lines warmth;
- annotation prepends a Fish S2 ``[cue]`` marker ONLY in the TTS render path:
  the manifest commentary text that captions the video stays clean;
- annotation is deterministic, and the render records that it ran
  (``emotion_applied``/``emotion_version`` in the broadcast tts section).
"""

from __future__ import annotations

import io
import re
import wave

from sandboxer_v0.commentary_emotion import (
    EMOTION_ANNOTATION_VERSION,
    FISH_CUE_TEMPLATE,
    annotate,
    line_context,
)
from sandboxer_v0.tts import (
    DEFAULT_TTS_MODEL,
    FakeTtsAdapter,
    FishAudioTtsAdapter,
    render_commentary_audio,
)
from sandboxer_v0.video import build_video_manifest


def _cue(spoken: str) -> str | None:
    """Extract the leading Fish bracket cue from a spoken line, if any."""
    match = re.match(r"^(\[[^\]]+\])\s", spoken)
    return match.group(1) if match else None


def test_play_by_play_capture_line_gets_excitement_marker():
    spoken = annotate(
        "Goes for the flag!", "play_by_play",
        {"phase": "blue", "event_kind": "capture_attempt"},
    )
    assert spoken.startswith("[excited] ")
    assert spoken.endswith("Goes for the flag!")
    # Verified captures hype too.
    assert _cue(annotate("It is verified!", "play_by_play", {"phase": "blue", "event_kind": "submission"})) == "[excited]"


def test_analyst_blue_line_gets_analytic_marker():
    spoken = annotate(
        "A measured inspection.", "analyst", {"phase": "blue", "event_kind": "inspect"}
    )
    assert spoken.startswith("[calm, analytical] ")
    assert spoken.endswith("A measured inspection.")
    # Blue-phase defensive beats stay steady regardless of role.
    assert _cue(annotate("Defense promoted.", "play_by_play", {"phase": "blue", "event_kind": "promote"})) == "[calm, analytical]"


def test_red_phase_line_gets_tense_marker():
    spoken = annotate(
        "Hammering the gate now.", "play_by_play", {"phase": "red", "event_kind": "probe"}
    )
    assert spoken.startswith("[tense, urgent] ")
    assert spoken.endswith("Hammering the gate now.")
    # A capture beat outranks background tension: the flag landing is the hype.
    assert _cue(annotate("Goes for the flag!", "play_by_play", {"phase": "red", "event_kind": "capture_attempt"})) == "[excited]"


def test_interview_line_gets_warm_marker():
    spoken = annotate(
        "What a moment for you.", "analyst", {"phase": "interview", "event_kind": "other"}
    )
    assert spoken.startswith("[warm, conversational] ")
    assert spoken.endswith("What a moment for you.")
    assert _cue(annotate("Take us through it.", "play_by_play", {"phase": "interview", "event_kind": ""})) == "[warm, conversational]"


def test_unmatched_context_stays_verbatim():
    # Neutral beats (blue play-by-play narration, finish wraps, unknowns) are
    # never decorated: the clean text passes through byte-for-byte.
    plain = [
        ("The services are up.", "play_by_play", {"phase": "blue", "event_kind": "match_start"}),
        ("That closes the match.", "analyst", {"phase": "finalizing", "event_kind": "finish"}),
        ("Something happened.", "analyst", {}),
    ]
    for text, role, context in plain:
        assert annotate(text, role, context) == text
    assert annotate("", "play_by_play", {"phase": "red", "event_kind": "probe"}) == ""
    assert annotate("   ", "analyst", {"phase": "interview", "event_kind": ""}) == "   "


def test_annotation_is_deterministic_and_template_shaped():
    context = {"phase": "red", "event_kind": "capture_attempt"}
    first = annotate("Goes for the flag!", "play_by_play", context)
    assert first == annotate("Goes for the flag!", "play_by_play", context)
    assert first.startswith(FISH_CUE_TEMPLATE.format(cue="excited"))
    assert "{cue}" in FISH_CUE_TEMPLATE  # single constant controls the format


def test_line_context_derives_phase_and_event_kind():
    terminal = {
        "b1": {"event_id": "b1", "event_type": "MODEL_RESPONSE", "phase": "blue", "text": "inspect: declared surface"},
        "r1": {"event_id": "r1", "event_type": "MODEL_RESPONSE", "phase": "red", "text": "verify: objective token sent"},
        "i1": {"event_id": "i1", "event_type": "INTERVIEW_RECORDED", "phase": "interview", "text": "how did it feel"},
    }
    assert line_context({"event_ids": ["b1"]}, terminal_events=terminal) == {
        "phase": "blue", "event_kind": "inspect",
    }
    assert line_context({"event_ids": ["r1"]}, terminal_events=terminal)["event_kind"] == "capture_attempt"
    # Interview citations dominate any other cited frame's phase.
    mixed = line_context({"event_ids": ["b1", "i1"]}, terminal_events=terminal)
    assert mixed["phase"] == "interview"
    # Capture kinds win when several actions share one line.
    multi = line_context({"event_ids": ["b1", "r1"]}, terminal_events=terminal)
    assert multi["event_kind"] == "capture_attempt" and multi["phase"] == "red"
    # Recap detection falls back to the frame anchor past the recap scene.
    recap = line_context(
        {"event_ids": [], "start_frame": 95_000},
        terminal_events={}, recap_start_frame=90_000,
    )
    assert recap == {"phase": "recap", "event_kind": "other"}
    assert line_context({"event_ids": []}, terminal_events={}) == {"phase": "", "event_kind": "other"}


def _replay():
    return {"schema_version": "sandboxer.replay.v1", "source_bundle_hash": "a" * 64, "panes": [{"identity": "DeepSeek V4 Pro"}, {"identity": "MiMo V2.5 Pro"}], "layout": {"split": {"left": .5, "right": .5, "permanent": True}}, "frames": [
        {"sequence": 1, "at_monotonic_ns": 0, "event_id": "e1", "event_type": "MATCH_STARTED", "phase": "blue", "pane": 0, "text": "defend the service"},
        {"sequence": 2, "at_monotonic_ns": 2_000_000_000, "event_id": "e2", "event_type": "MODEL_RESPONSE", "phase": "blue", "pane": 1, "text": "inspect: declared surface"},
        {"sequence": 3, "at_monotonic_ns": 4_000_000_000, "event_id": "e3", "event_type": "MODEL_RESPONSE", "phase": "red", "pane": 0, "text": "verify: objective token sent"},
        {"sequence": 4, "at_monotonic_ns": 6_000_000_000, "event_id": "e4", "event_type": "INTERVIEW_RECORDED", "phase": "interview", "pane": 0, "text": "how did it feel"},
        {"sequence": 5, "at_monotonic_ns": 8_000_000_000, "event_id": "e5", "event_type": "MODEL_RESPONSE", "phase": "red", "pane": 1, "text": "probe: target health"},
        {"sequence": 6, "at_monotonic_ns": 10_000_000_000, "event_id": "e6", "event_type": "MATCH_FINISHED", "phase": "finalizing", "pane": 0, "text": ""},
    ]}


def _draft():
    return [
        {"voice_role": "play_by_play", "line_type": "observed", "event_ids": ["e1"], "text": "The services are up."},
        {"voice_role": "analyst", "line_type": "interpreted", "event_ids": ["e2"], "text": "A measured inspection."},
        {"voice_role": "play_by_play", "line_type": "observed", "event_ids": ["e3"], "text": "Goes for the flag!"},
        {"voice_role": "analyst", "line_type": "interpreted", "event_ids": ["e4"], "text": "What a moment for you."},
        {"voice_role": "play_by_play", "line_type": "observed", "event_ids": ["e5"], "text": "Hammering the gate now."},
        {"voice_role": "analyst", "line_type": "observed", "event_ids": ["e6"], "text": "That closes the match."},
    ]


_EXPECTED_CUES = {
    "The services are up.": None,               # neutral blue open
    "A measured inspection.": "[calm, analytical]",  # blue defense analysis
    "Goes for the flag!": "[excited]",          # capture attempt beat
    "What a moment for you.": "[warm, conversational]",  # interview
    "Hammering the gate now.": "[tense, urgent]",    # red-phase pressure
    "That closes the match.": None,             # neutral wrap
}


def _manifest(**kwargs):
    return build_video_manifest(
        _replay(), report={"report_url": "r", "outcome": {}},
        model_metadata={}, benchmark_snapshot={}, **kwargs,
    )


def _fish_wav(duration_ms: int = 100) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(24000)
        writer.writeframes(b"\x00\x00" * (24000 * duration_ms // 1000))
    return buffer.getvalue()


class _FishClient:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.calls: list[dict[str, str]] = []

    def tts(self, *, text: str, reference_id: str) -> bytes:
        self.calls.append({"text": text, "reference_id": reference_id})
        return self.data


def test_render_path_annotates_spoken_text_but_keeps_manifest_clean(tmp_path):
    manifest = _manifest(commentary=_draft())
    clean_before = [str(line["text"]) for line in manifest["commentary"]]
    client = _FishClient(_fish_wav())
    adapter = FishAudioTtsAdapter(client=client, reference_ids={"Kore": "ref-k", "Charon": "ref-c"}, probe=False)
    rendered = render_commentary_audio(manifest, adapter, out_dir=tmp_path)
    # Every scheduled line keeps its clean caption text...
    assert [str(line["text"]) for line in manifest["commentary"]] == clean_before
    # ...while the text sent to Fish carries exactly the expected cue.
    spoken_by_clean: dict[str, str] = {}
    for call in client.calls:
        match = re.match(r"^(\[[^\]]+\])\s(.*)$", str(call["text"]), re.DOTALL)
        clean = match.group(2) if match else str(call["text"])
        assert clean not in spoken_by_clean, "each line synthesized exactly once"
        spoken_by_clean[clean] = str(call["text"])
    assert set(spoken_by_clean) == set(clean_before)
    for clean, spoken in spoken_by_clean.items():
        assert _cue(spoken) == _EXPECTED_CUES[clean], (clean, spoken)
    # Block records describe what was actually spoken (cued scripts).
    for block in rendered["blocks"]:
        clean = re.sub(r"^\[[^\]]+\]\s", "", str(block["script"]))
        assert _cue(str(block["script"])) == _EXPECTED_CUES[clean]
    # Provenance: the tts section records that emotion annotations were applied.
    assert rendered["emotion_applied"] is True
    assert rendered["emotion_version"] == EMOTION_ANNOTATION_VERSION


def test_render_is_deterministic_with_annotations(tmp_path):
    first = render_commentary_audio(
        _manifest(commentary=_draft()),
        FishAudioTtsAdapter(client=_FishClient(_fish_wav()), reference_ids={"Kore": "ref-k", "Charon": "ref-c"}, probe=False),
        out_dir=tmp_path,
    )
    second = render_commentary_audio(
        _manifest(commentary=_draft()),
        FishAudioTtsAdapter(client=_FishClient(_fish_wav()), reference_ids={"Kore": "ref-k", "Charon": "ref-c"}, probe=False),
        out_dir=tmp_path,
    )
    assert second["blocks_hash"] == first["blocks_hash"]
    assert [block["script"] for block in second["blocks"]] == [block["script"] for block in first["blocks"]]


def test_non_fish_providers_speak_verbatim_without_cues(tmp_path):
    # Annotation is Fish-inline-syntax specific: other providers (and dry-run
    # fakes) must never receive bracket cues, and the section says so.
    manifest = _manifest(commentary=_draft())
    # Short synthetic durations keep the packed schedule inside the episode
    # (the default ~80ms/char would push blocks past the recap boundary).
    adapter = FakeTtsAdapter(ms_per_char=10, min_ms=60)
    rendered = render_commentary_audio(manifest, adapter, out_dir=tmp_path)
    assert [call["script"] for call in adapter.calls] == [str(line["text"]) for line in manifest["commentary"]]
    assert rendered["emotion_applied"] is False
    assert rendered["emotion_version"] == EMOTION_ANNOTATION_VERSION
    # The gate is provider-based, not adapter-class-based: a fake standing in
    # for the fish provider speaks the same cued text Fish would.
    fish_like = FakeTtsAdapter(model=DEFAULT_TTS_MODEL, provider="fish", ms_per_char=10, min_ms=60)
    rendered_fish_like = render_commentary_audio(manifest, fish_like, out_dir=tmp_path / "fish-like")
    assert rendered_fish_like["emotion_applied"] is True
    assert any(str(call["script"]).startswith("[") for call in fish_like.calls)
