"""Build broadcast artifacts from a real Command Code match.

Reads a frozen match's telemetry JSONL + result.json (as written by
`run_command_code_match.py`), converts them through the canonical
`sandboxer_v0.artifact_converter.convert` phase, and produces, under artifacts/:

  replay.json            (schema sandboxer.replay.v1)
  report.json            (schema sandboxer.result-report.v1)
  video-manifest.json    (sandboxer.video-manifest.v1, via build_video_manifest)
  remotion-props.json    ({manifest: video-manifest.json})

Commentary, intro and arena-plan are drafted with the phase-configured
headless coding agent (defaults per phase; set SANDBOXER_COMMENTARY_AGENT /
SANDBOXER_INTRO_AGENT / SANDBOXER_ARENA_AGENT to switch, e.g. ``hermes``),
with the deterministic fallbacks kept for fail-closed operation.

Usage (as the pilot user):

    python scripts/build_real_artifacts.py \
      --telemetry /var/lib/sandboxer/evidence/<match>.telemetry.jsonl \
      --result /var/lib/sandboxer/evidence/<match>.result.json

Pass ``--run-id`` (with optional ``--artifacts-root``, default ``--out-dir``)
to isolate outputs under ``<artifacts-root>/runs/<run-id>/`` using
``sandboxer_v0.run_layout`` canonical names plus a ``run-manifest.json``;
without those flags the legacy flat layout is unchanged.

Then render TTS + video as usual (Fish Audio is the default TTS provider):

    python scripts/render_commentary_audio.py
    cd ../video && npx remotion render src/index.tsx SandboxerSeries \
        ../artifacts/video-only.mp4 --props=../artifacts/remotion-props.json \
        --concurrency="$(python3 -c 'import sys; sys.path.insert(0, "../pilot"); from sandboxer_v0.render_preflight import preflight_render; print(preflight_render().concurrency)')"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PILOT_ROOT = Path(__file__).resolve().parents[1]
if str(PILOT_ROOT) not in sys.path:
    sys.path.insert(0, str(PILOT_ROOT))

from sandboxer_v0.artifact_converter import (
    IDENTITY_A,
    IDENTITY_B,
    _PANE_NAMES,
    _public_identity,
    convert,
)
from sandboxer_v0.arena_visual import FakeArenaVisualDrafter, draft_arena_plan
from sandboxer_v0.commentary import HeadlessIntroCommentaryDrafter, draft_intro_commentary, draft_commentary
from sandboxer_v0.run_layout import get_run_layout, run_artifact_path, write_run_manifest
from sandboxer_v0.video import _digest, build_video_manifest

ROOT = Path(__file__).resolve().parent.parent.parent / "artifacts"


def build_report(result: dict) -> dict:
    """A minimal result-report object the manifest/report layers can consume."""
    winner = _public_identity(str(result.get("winner", "")))
    outcome = str(result.get("outcome", ""))
    reason = str(result.get("reason_code", ""))
    captures = list(result.get("captures", []))
    defense_notes = []
    for model, defense in (result.get("defenses") or {}).items():
        name = _public_identity(model)
        policy = (defense or {}).get("protected_policy", "?")
        recovery = (defense or {}).get("recovery_posture", "?")
        defense_notes.append(f"{name} promoted a {policy}/{recovery} defense")
    return {
        "schema": "sandboxer.result-report.v1",
        "report_url": "https://sandboxer.example/reports/e2e-hermes",
        "outcome": {
            "winner": winner,
            "basis": f"{outcome} ({reason}). " + "; ".join(defense_notes),
        },
        "technical_chapters": [{"match_number": 1, "title": "Match 1 - capture race"}],
        "corrections": {"visible_notice": "No corrections recorded for this fixture."},
        "disclaimer": "Sandboxer is an experimental simulated-CTF showcase. No real systems were targeted.",
        "captures": captures,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--telemetry", type=Path, required=True,
                        help="match telemetry JSONL (run_command_code_match output)")
    parser.add_argument("--result", type=Path, required=True,
                        help="match result.json (run_command_code_match output)")
    parser.add_argument("--out-dir", type=Path, default=ROOT)
    parser.add_argument("--run-id", default=None,
                        help="isolate outputs under <artifacts-root>/runs/<run-id>/ (sandboxer_v0.run_layout)")
    parser.add_argument("--artifacts-root", type=Path, default=None,
                        help="artifacts root for --run-id; defaults to --out-dir")
    args = parser.parse_args(argv)

    layout = None
    out_dir = args.out_dir
    if args.run_id is not None:
        layout = get_run_layout(args.artifacts_root or args.out_dir, args.run_id)
        out_dir = layout.directory

    telemetry = [
        json.loads(line)
        for line in args.telemetry.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    result = json.loads(args.result.read_text(encoding="utf-8"))

    _evidence, canonical_replay = convert(telemetry, result)
    replay = canonical_replay.to_dict()
    report = build_report(result)

    model_metadata = {
        IDENTITY_A: {"producer": "Poolside", "architecture": "Mixture-of-Experts", "context_length": 128_000},
        IDENTITY_B: {"producer": "Meta", "architecture": "Mixture-of-Experts", "context_length": 128_000},
    }
    benchmark_snapshot = {
        "CTF-Bench": {IDENTITY_A: 0.71, IDENTITY_B: 0.68},
        "Terminal Reasoning": {IDENTITY_A: 0.83, IDENTITY_B: 0.80},
    }

    # Arena choreography.  This is kept deterministic on purpose: the
    # renderer reproduces the same geometry/timing from the same replay, so
    # the rehearsal drafter (no model) is the right tool even when a text
    # model is available for the other phases.  It maps every Blue
    # hardening event to a defense artifact and every Red event to an
    # attack beat.
    arena = draft_arena_plan(replay, report, drafter=FakeArenaVisualDrafter())
    print("arena: deterministic plan ok", file=sys.stderr)

    # Greeting / model intro (LLM draft with authored fallback).
    intro_facts = {
        "models": {
            IDENTITY_A: {**model_metadata[IDENTITY_A],
                         "expectation": "a hundred-billion-parameter coder that trades blows with far larger models"},
            IDENTITY_B: {**model_metadata[IDENTITY_B],
                         "expectation": "a closed frontier model with strong agentic-benchmark results"},
        },
        "benchmark_snapshot": benchmark_snapshot,
    }
    try:
        intro = draft_intro_commentary([IDENTITY_A, IDENTITY_B], intro_facts,
                                       drafter=HeadlessIntroCommentaryDrafter())
        print("intro: LLM draft ok", file=sys.stderr)
    except Exception as error:
        print(f"intro LLM draft failed ({error}); using authored fallback", file=sys.stderr)
        intro = [
            {"voice_role": "play_by_play", "line_type": "editorial", "scene": "cold_open", "offset_seconds": 3.0,
             "text": "Welcome back to Sandboxer."},
            {"voice_role": "analyst", "line_type": "editorial", "scene": "cold_open", "offset_seconds": 5.5,
             "text": "Two isolated models, one flag each. No way out but through."},
            {"voice_role": "play_by_play", "line_type": "editorial", "scene": "model_cards_and_rules",
             "offset_seconds": 1.0, "model": IDENTITY_A,
             "text": "In violet, Laguna S 2.1 - Poolside's hundred-billion-parameter coder."},
            {"voice_role": "analyst", "line_type": "editorial", "scene": "model_cards_and_rules",
             "offset_seconds": 4.5,
             "text": "It runs on a desktop, trading blows with models ten times its size."},
            {"voice_role": "play_by_play", "line_type": "editorial", "scene": "model_cards_and_rules",
             "offset_seconds": 8.0, "model": IDENTITY_B,
             "text": "In blue, Muse Spark 1.2 - Meta's first closed frontier model."},
            {"voice_role": "analyst", "line_type": "interpreted", "scene": "model_cards_and_rules",
             "offset_seconds": 11.5,
             "text": "Strong on agentic benchmarks like CyberGym, it seems Muse moves fast."},
        ]

    # Two-voice commentary (LLM draft with deterministic fallback).
    try:
        commentary = draft_commentary(replay, report)
        print(f"commentary: LLM draft ok ({len(commentary)} lines)", file=sys.stderr)
    except Exception as error:
        print(f"commentary LLM draft failed ({error}); using authored fallback", file=sys.stderr)
        winner = report["outcome"]["winner"]
        replay_frames = replay.get("frames", [])
        first_id = str(replay_frames[0]["event_id"]) if replay_frames else "e01"
        last_id = str(replay_frames[-1]["event_id"]) if replay_frames else "e01"
        commentary = [
            {"voice_role": "play_by_play", "line_type": "observed", "event_ids": [first_id],
             "text": f"We're live. The arena is up, {_PANE_NAMES[0]} and {_PANE_NAMES[1]} are in their corners."},
            {"voice_role": "analyst", "line_type": "interpreted", "event_ids": [last_id],
             "text": f"It looks like the match came down to strategy, and {winner} came out ahead."},
        ]

    manifest = build_video_manifest(
        replay,
        report=report,
        model_metadata=model_metadata,
        benchmark_snapshot=benchmark_snapshot,
        fps=30,
        commentary=commentary,
        arena_visuals=arena,
        intro_commentary=intro,
    )

    # Renderer-side terminal sidecar (same convention as build_synthetic_artifacts).
    start_ns = int(replay["frames"][0]["at_monotonic_ns"])
    match_start = min(int(f["at_monotonic_ns"]) for f in replay["frames"])
    scene_offsets: dict[int, int] = {}
    cursor = manifest["scenes"][0]["duration_frames"] + manifest["scenes"][1]["duration_frames"]
    for scene in manifest["scenes"][2:]:
        if scene["type"] == "match":
            scene_offsets[scene["match_number"]] = cursor
        cursor += scene["duration_frames"]
    terminals = []
    for frame in replay["frames"]:
        at_frame = scene_offsets[1] + round((int(frame["at_monotonic_ns"]) - match_start) / 1_000_000_000 * 30)
        terminals.append({
            "at_frame": at_frame,
            "pane": frame["pane"],
            "phase": frame["phase"],
            "event_type": frame["event_type"],
            "text": frame["text"],
            "event_id": frame["event_id"],
        })
    manifest["terminal"] = terminals
    manifest["manifest_hash"] = _digest(manifest)

    out_dir.mkdir(parents=True, exist_ok=True)
    replay_path = run_artifact_path(out_dir, "replay") if layout else out_dir / "replay.json"
    report_path = run_artifact_path(out_dir, "report") if layout else out_dir / "report.json"
    vmanifest_path = run_artifact_path(out_dir, "video_manifest") if layout else out_dir / "video-manifest.json"
    replay_path.write_text(json.dumps(replay, indent=2), encoding="utf-8")
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    vmanifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (out_dir / "remotion-props.json").write_text(
        json.dumps({"manifest": manifest}, indent=2), encoding="utf-8")
    if layout is not None:
        write_run_manifest(out_dir, args.run_id,
                           {"telemetry": args.telemetry, "result": args.result})
        print(f"wrote {layout.manifest_path}")
    total_frames = sum(scene["duration_frames"] for scene in manifest["scenes"])
    print(f"wrote {replay_path} ({len(replay['frames'])} frames)")
    print(f"wrote {report_path}")
    print(f"wrote {vmanifest_path} - {len(manifest['scenes'])} scenes, "
          f"{total_frames} frames @ {manifest['fps']}fps = {total_frames / manifest['fps']:.1f}s, "
          f"{len(manifest['commentary'])} commentary lines")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())