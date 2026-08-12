from __future__ import annotations

from typing import Any

import pytest

from sandboxer_v0 import (
    ControlledCompetitor,
    FakeModelAdapter,
    FakeRunnerBackend,
    MatchPolicy,
    SeriesSpec,
    execute_series,
)
from sandboxer_v0.replay import ReplayError, _digest, build_replay, sanitize_terminal_text


def _spec() -> SeriesSpec:
    return SeriesSpec(
        schema_version="sandboxer.series-spec.v1",
        series_id="replay-fixture",
        seed="replay-seed",
        competitors=(
            ControlledCompetitor("atlas", FakeModelAdapter("atlas-model", ("defend", "capture"))),
            ControlledCompetitor("borealis", FakeModelAdapter("borealis-model", ("defend", "finish"))),
        ),
        match_policy=MatchPolicy(best_of=3, output_token_budget=100, turn_budget=2, tool_budget=2),
        runner_backend=FakeRunnerBackend(),
    )


def test_replay_is_derived_from_one_frozen_evidence_version_and_keeps_timing() -> None:
    bundle = execute_series(_spec()).evidence_bundle

    replay = build_replay(bundle, renderer_version="terminal-renderer.v1", asset_version="assets.v1", font_version="font.v1")

    assert replay["schema_version"] == "sandboxer.replay.v1"
    assert replay["source_evidence"] == {"url": "sandboxer://evidence/replay-fixture/v1", "version": 1}
    assert replay["layout"]["split"] == {"left": 0.5, "right": 0.5, "permanent": True}
    assert [pane["identity"] for pane in replay["panes"]] == ["atlas", "borealis"]
    assert replay["renderer"]["reduced_motion"]["transitions"] == "none"
    frames = replay["frames"]
    assert [frame["sequence"] for frame in frames] == list(range(1, len(frames) + 1))
    assert [frame["at_monotonic_ns"] for frame in frames] == sorted(frame["at_monotonic_ns"] for frame in frames)
    assert replay == build_replay(bundle, renderer_version="terminal-renderer.v1", asset_version="assets.v1", font_version="font.v1")


def test_replay_sanitizes_ansi_links_credentials_flags_paths_and_private_reasoning() -> None:
    bundle = execute_series(_spec()).evidence_bundle
    telemetry = list(bundle["public"]["normalized_telemetry"])
    telemetry.append(
        {
            "event_id": "adversarial",
            "event_type": "MODEL_RESPONSE",
            "event_kind": "MODEL_CLAIMED",
            "phase": "red",
            "turn": 99,
            "competitor": "atlas",
            "wall_time_utc": "2026-01-01T00:00:00+00:00",
            "orchestrator_monotonic_ns": 99999999999,
            "terminal_text": "SAFE-BEGIN \x1b]8;;https://evil.example\x07Bearer sk-live-123456789 secret flag{never-publish} /home/private/.env SAFE-END private reasoning: do not show 10.0.0.4",
        }
    )
    tampered: dict[str, Any] = dict(bundle)
    tampered["public"] = dict(bundle["public"], normalized_telemetry=tuple(telemetry))
    tampered["checksums"] = dict(bundle["checksums"], public=_digest(tampered["public"]))

    replay = build_replay(tampered)
    rendered = repr(replay)
    assert "evil.example" not in rendered
    assert "sk-live-123456789" not in rendered
    assert "never-publish" not in rendered
    assert "/home/private" not in rendered
    assert "10.0.0.4" not in rendered
    assert "private reasoning" not in rendered.lower()
    assert "\x1b" not in rendered
    assert "SAFE-BEGIN" in rendered
    assert "SAFE-END" in rendered


def test_private_domain_redaction_preserves_surrounding_text_and_all_domains() -> None:
    text = "before service.internal and api.local after; keep internalized.locality and .local suffix"

    sanitized = sanitize_terminal_text(text)

    assert "before" in sanitized and "after" in sanitized
    assert "service.internal" not in sanitized
    assert "api.local" not in sanitized
    assert "internalized.locality" in sanitized
    assert ".local suffix" in sanitized


def test_replay_rejects_unfrozen_or_internal_alias_input() -> None:
    with pytest.raises(ReplayError, match="FROZEN_EVIDENCE_REQUIRED"):
        build_replay({"schema_version": "sandboxer.match-telemetry.v1"})

    bundle = execute_series(_spec()).evidence_bundle
    spec = dict(bundle["public"]["specification"])
    competitors = [dict(item) for item in spec["competitors"]]
    competitors[0]["public_name"] = "Alpha"
    altered = dict(bundle, public=dict(bundle["public"], specification=dict(spec, competitors=tuple(competitors))))
    altered["checksums"] = dict(bundle["checksums"], public=_digest(altered["public"]))
    replay = build_replay(altered)
    assert "Alpha" not in repr(replay)
    assert replay["panes"][0]["identity"] == "atlas-model"
