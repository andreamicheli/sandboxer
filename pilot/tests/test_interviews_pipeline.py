"""End-to-end interview surfacing: runtime telemetry -> canonical artifacts -> video manifest."""

from __future__ import annotations

from sandboxer_v0.artifact_converter import convert, validate_artifacts, write_artifacts
from sandboxer_v0.video import build_video_manifest

from test_artifact_converter import _result, _telemetry

REPORT = {"report_url": "r", "outcome": {}}

TWO_INTERVIEW_LINES = (
    {
        "monotonic_ns": 4_500_000_000, "kind": "interview_line",
        "model": "poolside/laguna-s-2.1-free", "competitor": "Laguna S 2.1",
        "phase": "interview", "text": "I hardened the header policy first.",
    },
    {
        "monotonic_ns": 4_700_000_000, "kind": "interview_line",
        "model": "meta/muse-spark-1.2-contributor", "competitor": "Muse Spark 1.2",
        "phase": "interview", "text": "I expect probing of the recovery path.",
    },
)


def _telemetry_with(interview_lines) -> list[dict]:
    """Base telemetry with interview_line records inserted before interview_finished."""
    telemetry = [dict(event) for event in _telemetry()]
    return telemetry[:5] + [dict(line) for line in interview_lines] + telemetry[5:]


def _windows(scenes):
    out = []
    cursor = 0
    for scene in scenes:
        out.append((scene, cursor, cursor + int(scene["duration_frames"])))
        cursor += int(scene["duration_frames"])
    return out


def test_interview_lines_convert_to_recorded_interview_frames():
    evidence, replay = convert(_telemetry_with(TWO_INTERVIEW_LINES), _result())

    recorded = [event for event in evidence.events if event["event_type"] == "INTERVIEW_RECORDED"]
    assert [(event["event_id"], event["phase"], event["competitor"], event["pane"]) for event in recorded] == [
        ("e06", "interview", "Laguna S 2.1", 0),
        ("e07", "interview", "Muse Spark 1.2", 1),
    ]
    assert [event["text"] for event in recorded] == [
        "I hardened the header policy first.",
        "I expect probing of the recovery path.",
    ]
    assert evidence.phases == ["blue", "interview", "red"]

    frames = [frame for frame in replay.frames if frame["phase"] == "interview"]
    assert [frame["at_monotonic_ns"] for frame in frames] == [4_500_000_000, 4_700_000_000]
    timestamps = [frame["at_monotonic_ns"] for frame in replay.frames]
    assert timestamps == sorted(timestamps)


def test_two_interview_lines_populate_manifest_interviews_and_insert_scenes(tmp_path):
    _, replay = convert(_telemetry_with(TWO_INTERVIEW_LINES), _result())
    manifest = build_video_manifest(
        replay.to_dict(), report=REPORT, model_metadata={}, benchmark_snapshot={},
    )
    fps = manifest["fps"]

    assert [scene["type"] for scene in manifest["scenes"]] == [
        "custom_intro", "model_cards_and_rules", "match",
        "interview_card", "interviews", "red_phase_card", "match", "factual_recap",
    ]
    assert manifest["scenes"][2]["scene_key"] == "match:1"
    assert manifest["scenes"][3]["duration_frames"] == 4 * fps
    interviews_scene = manifest["scenes"][4]
    assert interviews_scene["scene_key"] == "interviews:1"
    assert interviews_scene["editing"] == "fullscreen"
    assert interviews_scene["event_ids"] == ["e06", "e07"]
    # Two entries -> 6s per entry, under the 30s cap.
    assert interviews_scene["duration_frames"] == 12 * fps
    assert manifest["scenes"][5]["type"] == "red_phase_card"

    assert manifest["interviews"] == [
        {"competitor": "Laguna S 2.1", "text": "I hardened the header policy first.",
         "event_ids": ["e06"]},
        {"competitor": "Muse Spark 1.2", "text": "I expect probing of the recovery path.",
         "event_ids": ["e07"]},
    ]

    # Total frames fall out of the scenes sum (Remotion derives duration the same way).
    total_frames = sum(int(scene["duration_frames"]) for scene in manifest["scenes"])
    assert total_frames == 8 * fps + 20 * fps + 105 + 4 * fps + 12 * fps + 3 * fps + 120 + 12 * fps

    # Interview terminal events land inside the interviews scene window.
    by_event = {entry["event_id"]: entry for entry in manifest["terminal"]}
    owner_of = lambda event_id: next(
        scene for scene, start, end in _windows(manifest["scenes"])
        if start <= by_event[event_id]["at_frame"] < end
    )
    assert owner_of("e06") is interviews_scene and owner_of("e07") is interviews_scene

    # The converted artifacts stay schema-clean with the extra phase present.
    evidence, replay = convert(_telemetry_with(TWO_INTERVIEW_LINES), _result())
    write_artifacts(evidence, replay, tmp_path)
    assert validate_artifacts(tmp_path) == []


def test_interviews_scene_duration_caps_at_thirty_seconds():
    many_lines = tuple(
        {
            "monotonic_ns": 4_500_000_000 + index * 50_000_000, "kind": "interview_line",
            "model": model, "competitor": competitor, "phase": "interview",
            "text": f"line {index}",
        }
        for index, (model, competitor) in enumerate(
            [("poolside/laguna-s-2.1-free", "Laguna S 2.1"),
             ("meta/muse-spark-1.2-contributor", "Muse Spark 1.2")] * 3
        )
    )
    _, replay = convert(_telemetry_with(many_lines), _result())
    manifest = build_video_manifest(
        replay.to_dict(), report=REPORT, model_metadata={}, benchmark_snapshot={},
    )

    interviews_scene = next(scene for scene in manifest["scenes"] if scene["type"] == "interviews")
    assert len(manifest["interviews"]) == 6
    # 6 x 6s = 36s would overflow; the 30s cap wins.
    assert interviews_scene["duration_frames"] == 30 * manifest["fps"]


def test_no_interview_events_leaves_scene_flow_unchanged():
    evidence, replay = convert(_telemetry(), _result())
    manifest = build_video_manifest(
        replay.to_dict(), report=REPORT, model_metadata={}, benchmark_snapshot={},
    )

    assert evidence.phases == ["blue", "red"]
    assert all(frame["event_type"] != "INTERVIEW_RECORDED" for frame in replay.frames)
    assert manifest["interviews"] == []
    assert [scene["type"] for scene in manifest["scenes"]] == [
        "custom_intro", "model_cards_and_rules", "match", "red_phase_card", "match", "factual_recap",
    ]
