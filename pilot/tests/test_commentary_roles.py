"""Tests verifying Kore (play-by-play) is the primary caster and Charon (analyst) is supporter."""
from sandboxer_v0.commentary import build_commentary

FPS = 30
_IDENTITIES = ["Laguna S 2.1", "Muse Spark 1.2"]


def test_kore_primary_caster_role_dominance():
    """In a realistic match with multiple actions and dead-air gaps, Kore (play_by_play)
    must lead the commentary with >= 65% of the spoken lines, while Charon (analyst)
    serves as supporting commentator with <= 35% of lines.
    """
    # Generate 15 distinct events separated by 20-second pauses (triggering dead-air fillers)
    frames = []
    for i in range(15):
        t_ns = i * 20 * 1_000_000_000
        frames.append({
            "event_id": f"e_{i}",
            "phase": "blue" if i < 10 else "red",
            "pane": i % 2,
            "event_type": "MODEL_RESPONSE",
            "text": f"Model action telemetry observation step {i}.",
            "at_monotonic_ns": t_ns,
        })

    lines = build_commentary(frames, _IDENTITIES, fps=FPS)
    assert len(lines) >= 20

    play_by_play_count = sum(1 for line in lines if line["voice_role"] == "play_by_play")
    analyst_count = sum(1 for line in lines if line["voice_role"] == "analyst")

    total_lines = len(lines)
    pbp_ratio = play_by_play_count / total_lines
    analyst_ratio = analyst_count / total_lines

    # Kore must be primary (>= 65%) and Charon must be supporter (<= 35%)
    assert pbp_ratio >= 0.65, f"Play-by-play (Kore) ratio {pbp_ratio:.1%} is under 65%"
    assert analyst_ratio <= 0.35, f"Analyst (Charon) ratio {analyst_ratio:.1%} exceeds 35%"
    assert play_by_play_count > analyst_count * 1.8, "Kore must lead Charon by at least ~2:1 ratio"
