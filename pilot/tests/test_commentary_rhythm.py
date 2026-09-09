"""Tests for commentary rhythm, pauses, and heated-moment tempo."""
from sandboxer_v0.commentary import build_commentary, MIN_LINE_GAP_SECONDS

FPS = 30
_IDENTITIES = ["Laguna S 2.1", "Muse Spark 1.2"]


def test_commentary_rhythm_leaves_gap_in_calm_moments():
    """Non-heated lines must have a breathing pause before the next line."""
    frames = [
        {"event_id": "e1", "phase": "blue", "pane": 0, "event_type": "MODEL_RESPONSE",
         "text": "Laguna inspects the environment.", "at_monotonic_ns": 0},
        {"event_id": "e2", "phase": "blue", "pane": 1, "event_type": "MODEL_RESPONSE",
         "text": "Muse reviews system posture.", "at_monotonic_ns": 5_000_000_000},  # 5s later
    ]
    lines = build_commentary(frames, _IDENTITIES, fps=FPS)
    assert len(lines) >= 2

    # Line 0 starts at 0, Line 1 starts at 5s (frame 150)
    # Line 0 end_frame should be <= next_start - gap
    expected_gap_frames = int(round(FPS * MIN_LINE_GAP_SECONDS))
    gap = lines[1]["start_frame"] - lines[0]["end_frame"]
    assert gap >= expected_gap_frames, f"Expected gap >= {expected_gap_frames} frames, got {gap}"


def test_commentary_rhythm_keeps_zero_gap_in_heated_red_phase():
    """Heated moments (red phase / capture beats) must maintain tight zero-gap rhythm."""
    frames = [
        {"event_id": "e1", "phase": "red", "pane": 0, "event_type": "MODEL_RESPONSE",
         "text": "Laguna launches exploit payload.", "at_monotonic_ns": 0},
        {"event_id": "e2", "phase": "red", "pane": 1, "event_type": "MODEL_RESPONSE",
         "text": "Muse counters the exploit attempt.", "at_monotonic_ns": 2_000_000_000},  # 2s later
    ]
    lines = build_commentary(frames, _IDENTITIES, fps=FPS)
    assert len(lines) >= 2

    # In red phase, both are heated -> gap should be 0 (end_frame touches next start_frame)
    gap = lines[1]["start_frame"] - lines[0]["end_frame"]
    assert gap == 0, f"Expected 0 gap in heated red phase, got {gap}"
