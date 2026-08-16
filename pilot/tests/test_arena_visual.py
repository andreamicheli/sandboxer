"""Tests for sandboxer_v0.arena_visual (arena choreography drafting + validation)."""

from __future__ import annotations

import json

import pytest

from sandboxer_v0.arena_visual import (
    ArenaVisualError,
    FakeArenaVisualDrafter,
    GeminiArenaVisualDrafter,
    draft_arena_plan,
    validate_arena_plan,
)

IDENTITIES = ("Laguna S 2.1", "Muse Spark 1.2")


def _replay() -> dict:
    return {
        "schema_version": "sandboxer.replay.v1",
        "panes": [{"identity": IDENTITIES[0]}, {"identity": IDENTITIES[1]}],
        "frames": [
            {"event_id": "e01", "at_monotonic_ns": 1_000_000_000, "phase": "blue", "pane": 0, "text": "hardening: patched weak cipher list", "match_number": 1},
            {"event_id": "e02", "at_monotonic_ns": 2_000_000_000, "phase": "blue", "pane": 1, "text": "hardening: disabled directory listing", "match_number": 1},
            {"event_id": "e03", "at_monotonic_ns": 3_000_000_000, "phase": "red", "pane": 0, "text": "candidate: crafted input on /login?user=", "match_number": 1},
            {"event_id": "e04", "at_monotonic_ns": 4_000_000_000, "phase": "red", "pane": 1, "text": "objective token retrieved", "match_number": 1},
        ],
    }


def _report() -> dict:
    return {"outcome": {"winner": IDENTITIES[0], "basis": "test"}}


def test_fake_drafter_produces_valid_plan():
    plan = draft_arena_plan(_replay(), _report(), drafter=FakeArenaVisualDrafter())
    assert plan["schema"] == "sandboxer.arena-visual-plan.v1"
    assert [a["competitor"] for a in plan["avatars"]] == list(IDENTITIES)
    assert plan["defenses"]
    assert plan["beats"]
    # every attack beat is grounded to a red-phase event and targets the opponent.
    for beat in plan["beats"]:
        if beat["type"] == "attack":
            assert beat["event_ids"]
            assert beat["attacker"] != plan["defenses"][0]["competitor"] or beat["target"] != plan["defenses"][0]["id"]


def test_validate_rejects_unknown_competitor():
    plan = draft_arena_plan(_replay(), _report(), drafter=FakeArenaVisualDrafter())
    plan["avatars"][0]["competitor"] = "Nope"
    failures = validate_arena_plan(plan, _replay()["frames"], IDENTITIES)
    assert any("unknown competitor" in f or "exactly the two" in f for f in failures)


def test_validate_rejects_ungrounded_attack():
    plan = draft_arena_plan(_replay(), _report(), drafter=FakeArenaVisualDrafter())
    attack = next(b for b in plan["beats"] if b["type"] == "attack")
    attack["event_ids"] = ["does-not-exist"]
    failures = validate_arena_plan(plan, _replay()["frames"], IDENTITIES)
    assert any("grounded to real event_ids" in f for f in failures)


def test_validate_rejects_self_attack():
    plan = draft_arena_plan(_replay(), _report(), drafter=FakeArenaVisualDrafter())
    attack = next(b for b in plan["beats"] if b["type"] == "attack")
    # point the attack at the attacker's own defense
    attack["target"] = next(d["id"] for d in plan["defenses"] if d["competitor"] == attack["attacker"])
    failures = validate_arena_plan(plan, _replay()["frames"], IDENTITIES)
    assert any("own defense" in f for f in failures)


class _FakeClient:
    def __init__(self, text: str):
        self._text = text
        self.models = self

    def generate_content(self, *, model: str, contents: str):
        return _FakeResponse(self._text)


class _FakeResponse:
    def __init__(self, text: str):
        self.text = text


def test_gemini_parse_handles_markdown_fence_and_eventid_in_start_frame():
    drafter = GeminiArenaVisualDrafter(
        client=_FakeClient(
            "```json\n"
            + json.dumps(
                {
                    "avatars": [
                        {"competitor": IDENTITIES[0], "label": "LAG", "shape": "hexagon"},
                        {"competitor": IDENTITIES[1], "label": "MUS", "shape": "diamond"},
                    ],
                    "defenses": [
                        {"id": "d0", "competitor": IDENTITIES[0], "label": "cipher", "shape": "cube", "event_ids": ["e01"]},
                        {"id": "d1", "competitor": IDENTITIES[1], "label": "dir", "shape": "shield", "event_ids": ["e02"]},
                    ],
                    "beats": [
                        {"type": "build", "defense": "d0", "start_frame": "e01"},
                        {"type": "build", "defense": "d1", "start_frame": 12.0},
                        {"type": "attack", "attacker": IDENTITIES[0], "target": "d1", "kind": "parameter_probe", "outcome": "breached", "start_frame": "e03", "event_ids": ["e03"]},
                    ],
                }
            )
            + "\n```"
        )
    )
    plan = drafter.draft(_replay(), _report())
    # non-numeric start_frame strings are normalized to ints; the fence is stripped.
    assert isinstance(plan["beats"][0]["start_frame"], int)
    assert plan["beats"][1]["start_frame"] == 12
    assert plan["beats"][2]["start_frame"] == 0


def test_gemini_parse_rejects_non_json():
    drafter = GeminiArenaVisualDrafter(client=_FakeClient("sorry, I can't do that"))
    with pytest.raises(ArenaVisualError) as exc:
        drafter.draft(_replay(), _report())
    assert "PARSE" in str(exc.value)


def test_gemini_parse_rejects_ungrounded_draft():
    drafter = GeminiArenaVisualDrafter(
        client=_FakeClient(
            json.dumps(
                {
                    "avatars": [{"competitor": IDENTITIES[0], "label": "A", "shape": "circle"}],
                    "defenses": [{"id": "d0", "competitor": IDENTITIES[0], "label": "x", "shape": "cube"}],
                    "beats": [{"type": "attack", "attacker": IDENTITIES[0], "target": "d0", "kind": "direct", "outcome": "blocked", "start_frame": 0, "event_ids": ["nope"]}],
                }
            )
        )
    )
    with pytest.raises(ArenaVisualError) as exc:
        drafter.draft(_replay(), _report())
    assert "ARENA_VISUAL_INVALID" in str(exc.value)
