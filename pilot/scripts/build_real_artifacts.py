"""Build broadcast artifacts from real Command Code matches.

Reads a frozen match's telemetry JSONL + result.json (as written by
`run_command_code_match.py`), converts them through the canonical
`sandboxer_v0.artifact_converter.convert` phase, and produces, under artifacts/:

  replay.json            (schema sandboxer.replay.v1)
  report.json            (schema sandboxer.result-report.v1)
  video-manifest.json    (sandboxer.video-manifest.v1, via build_video_manifest)
  remotion-props.json    ({manifest: video-manifest.json})

With ``--series`` the same pipeline consumes THREE matches (a best-of-3
episode produced by ``scripts/run_series.py``) and emits ONE combined set of
artifacts whose video manifest contains all three matches sequentially
(intermission cards between them), plus ``<series-id>.series.json`` with the
per-match winners and the series decision.

Commentary, intro and arena-plan are drafted with the phase-configured
headless coding agent (defaults per phase; set SANDBOXER_COMMENTARY_AGENT /
SANDBOXER_INTRO_AGENT / SANDBOXER_ARENA_AGENT to switch, e.g. ``hermes``),
with the deterministic fallbacks kept for fail-closed operation.

Usage (as the pilot user):

    python scripts/build_real_artifacts.py \
      --telemetry /var/lib/sandboxer/evidence/<match>.telemetry.jsonl \
      --result /var/lib/sandboxer/evidence/<match>.result.json

    python scripts/build_real_artifacts.py --series \
      --telemetry <m1>.telemetry.jsonl <m2>.telemetry.jsonl <m3>.telemetry.jsonl \
      --result <m1>.result.json <m2>.result.json <m3>.result.json \
      [--series-id episode-v8] [--require-valid N] [--force]

Series mode enforces a strict gate: unless at least ``--require-valid``
(default 2) matches recorded ``result=passed`` with outcome
``VALID_CAPTURE``, nothing is written and the build exits nonzero with
``SERIES_INSUFFICIENT_VALID_MATCHES`` (bypass with ``--force``).

Pass ``--run-id`` (with optional ``--artifacts-root``, default ``--out-dir``)
to isolate outputs under ``<artifacts-root>/runs/<run-id>/`` using
``sandboxer_v0.run_layout`` canonical names plus a ``run-manifest.json``;
without those flags the legacy flat layout is unchanged.

Head-to-head benchmark rows come from a dataset file (default
``pilot/data/benchmark_snapshot.json``, override with ``--benchmark-data``);
the section matching this match's two identities is selected automatically.

Then render TTS + video as usual (Fish Audio is the default TTS provider):

    python scripts/render_commentary_audio.py
    cd ../video && npx remotion render src/index.tsx SandboxerSeries \
        ../artifacts/video-only.mp4 --props=../artifacts/remotion-props.json \
        --concurrency="$(python3 -c 'import sys; sys.path.insert(0, "../pilot"); from sandboxer_v0.render_preflight import preflight_render; print(preflight_render().concurrency)')"
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

PILOT_ROOT = Path(__file__).resolve().parents[1]
if str(PILOT_ROOT) not in sys.path:
    sys.path.insert(0, str(PILOT_ROOT))

from sandboxer_v0.artifact_converter import (
    IDENTITY_A,
    IDENTITY_B,
    REPLAY_SCHEMA,
    _public_identity,
    convert,
)
from sandboxer_v0.arena_visual import FakeArenaVisualDrafter, draft_arena_plan
from sandboxer_v0.commentary import (
    HeadlessIntroCommentaryDrafter,
    build_commentary,
    draft_intro_commentary,
)
from sandboxer_v0.run_layout import get_run_layout, run_artifact_path, write_run_manifest
from sandboxer_v0.series_result import build_series_summary, series_report_outcome
from sandboxer_v0.video import build_video_manifest

ROOT = Path(__file__).resolve().parent.parent.parent / "artifacts"

# Head-to-head benchmark rows live in a versioned data file (one section per
# pair, dual-published values only — see docs/benchmark-dataset-series001.md).
DEFAULT_BENCHMARK_DATA = PILOT_ROOT / "data" / "benchmark_snapshot.json"

SERIES_MATCHES = 3


class BenchmarkDataError(RuntimeError):
    """The benchmark dataset is missing or unreadable."""


class ArtifactInputError(ValueError):
    """The telemetry/result file inputs do not fit the requested mode."""


def count_valid_capture_results(result_docs: list[dict]) -> int:
    """Matches recorded as ``result=passed`` with outcome ``VALID_CAPTURE``."""
    return sum(1 for doc in result_docs
               if isinstance(doc, dict)
               and doc.get("result") == "passed"
               and doc.get("outcome") == "VALID_CAPTURE")


def _pair_slug(name: str) -> str:
    """Normalize an identity or pair key so display names and slugs match."""
    return re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")


def load_benchmark_snapshot(path: Path, identity_a: str, identity_b: str) -> dict[str, dict[str, float]]:
    """Select this match's pair section from the benchmark dataset file.

    Returns ``{benchmark: {identity_a: value, identity_b: value}}`` — the same
    shape the hardcoded snapshot used to have.  Pair keys match by identity
    names in either order (slug-normalized); an unknown pair yields an empty
    snapshot so a missing section can never silently borrow another pair's
    numbers.  Rows without a published score for both competitors are skipped.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise BenchmarkDataError(f"benchmark data file not found: {path}") from error
    except json.JSONDecodeError as error:
        raise BenchmarkDataError(f"benchmark data file is not valid JSON ({path}): {error}") from error
    pairs = data.get("pairs") if isinstance(data, dict) else None
    if not isinstance(pairs, dict):
        return {}
    wanted = {_pair_slug(f"{identity_a} vs {identity_b}"),
              _pair_slug(f"{identity_b} vs {identity_a}")}
    section = next((entry for key, entry in pairs.items()
                    if isinstance(key, str) and _pair_slug(key) in wanted and isinstance(entry, dict)),
                   None)
    if section is None:
        return {}
    snapshot: dict[str, dict[str, float]] = {}
    for row in section.get("benchmarks") or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("benchmark", "")).strip()
        values = row.get("values")
        if not name or not isinstance(values, dict):
            continue
        value_a, value_b = values.get(identity_a), values.get(identity_b)
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in (value_a, value_b)):
            continue  # dual-published rows only
        snapshot[name] = {identity_a: float(value_a), identity_b: float(value_b)}
    return snapshot


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


def build_series_report(summary: dict, results: list[dict]) -> dict:
    """A result-report for a whole best-of-3 series (recap layer input)."""
    chapters = [
        {"match_number": entry["match_number"],
         "title": f"Match {entry['match_number']} - {str(entry.get('reason_code', '')).lower() or 'no reason recorded'}"}
        for entry in summary.get("matches", [])
    ]
    return {
        "schema": "sandboxer.result-report.v1",
        "report_url": "https://sandboxer.example/reports/e2e-hermes",
        "outcome": series_report_outcome(summary),
        "technical_chapters": chapters,
        "corrections": {"visible_notice": "No corrections recorded for this fixture."},
        "disclaimer": "Sandboxer is an experimental simulated-CTF showcase. No real systems were targeted.",
        "captures": [capture for result in results for capture in list(result.get("captures", []) or [])],
    }


def merge_series_replays(replays: list[dict]) -> dict:
    """Merge per-match canonical replays into one multi-match replay.

    ``build_video_manifest`` already renders every frame group with a distinct
    ``match_number`` as its own scene block (with intermission cards between
    consecutive matches), so the series pipeline is a merge, not new scene
    code.  The merge guarantees the invariants the manifest builder relies on:

    - ``match_number`` becomes 1..N (the converter always emits 1);
    - event ids are prefixed ``m<i>:`` so the manifest's terminal feed and
      commentary grounding stay unique across matches;
    - ``sequence`` is renumbered 1..total;
    - ``at_monotonic_ns`` is offset so the merged timeline is globally
      strictly increasing even when matches come from different clock bases
      (per-match relative spacing is preserved).

    The panes/layout of the first replay are authoritative (all matches share
    the two identities); the source bundle hash folds all match hashes.
    """
    if not replays:
        raise ArtifactInputError("SERIES_REPLAYS_EMPTY")
    for position, replay in enumerate(replays, start=1):
        if replay.get("schema_version") != REPLAY_SCHEMA:
            raise ArtifactInputError(f"SERIES_REPLAY_SCHEMA_INVALID:m{position}")
        if not replay.get("frames"):
            raise ArtifactInputError(f"SERIES_REPLAY_EMPTY:m{position}")
    frames: list[dict] = []
    previous_ns = -1
    bundle_hashes: list[str] = []
    sequence = 0
    for position, replay in enumerate(replays, start=1):
        bundle_hashes.append(str(replay.get("source_bundle_hash", "")))
        # Push this match's clock base past the previous match's last frame,
        # preserving intra-match relative spacing.
        offset_ns = max(0, previous_ns + 1 - int(replay["frames"][0]["at_monotonic_ns"]))
        for frame in replay["frames"]:
            sequence += 1
            at_ns = int(frame["at_monotonic_ns"]) + offset_ns
            previous_ns = max(previous_ns, at_ns)
            frames.append({
                **frame,
                "sequence": sequence,
                "event_id": f"m{position}:{frame['event_id']}",
                "match_number": position,
                "at_monotonic_ns": at_ns,
            })
    return {
        "schema_version": REPLAY_SCHEMA,
        "source_bundle_hash": sha256("|".join(bundle_hashes).encode("utf-8")).hexdigest(),
        "panes": deepcopy(replays[0]["panes"]),
        "layout": deepcopy(replays[0]["layout"]),
        "frames": frames,
    }


def _dense_commentary_for_match(replay: dict, match_number: int, identities: list[str], fps: int,
                                global_base_ns: int) -> list[dict]:
    """Per-match dense rundown re-anchored onto the merged timeline.

    Event ids are re-prefixed so grounding resolves against the merged replay.
    ``start_frame``/``end_frame`` are shifted from the match's own clock base
    onto the merged replay's base: ``build_video_manifest`` derives each
    line's absolute placement from ``start_frame`` minus its primary event's
    global-clock position, so expressing both on the same clock preserves the
    drafted analyst delays for every match (not just match 1).  Each match
    therefore keeps a self-contained commentary track anchored inside its own
    segment; no dead-air filler can leak across an intermission.
    """
    prefix = f"m{match_number}:"
    frames = replay.get("frames", [])
    if not frames:
        return []
    delta_frames = round((int(frames[0]["at_monotonic_ns"]) - global_base_ns) / 1_000_000_000 * fps)
    return [
        {**line,
         "event_ids": [f"{prefix}{event_id}" for event_id in line.get("event_ids", ())],
         "start_frame": int(line["start_frame"]) + delta_frames,
         "end_frame": int(line["end_frame"]) + delta_frames}
        for line in build_commentary(frames, identities, fps=fps)
    ]


def _derive_series_id(match_ids: list[str]) -> str:
    """Strip the ``-m<i>`` suffix from the first match id ('ep-v8-m1' -> 'ep-v8')."""
    match = re.match(r"^(.*?)-m\d+$", match_ids[0] if match_ids else "")
    return match.group(1) if match else "series"


def build_broadcast_artifacts(
    telemetry_docs: list[list[dict]],
    result_docs: list[dict],
    *,
    fps: int = 30,
    benchmark_data: Path = DEFAULT_BENCHMARK_DATA,
    intro_drafter=None,
    series: bool = False,
) -> dict:
    """Convert runtime telemetry+result documents into broadcast artifacts.

    Single-match mode (default) reproduces the historical behavior exactly.
    Series mode consumes exactly three matches and returns one combined
    artifact set plus the aggregated ``series`` summary record.

    Returns ``{"replay", "report", "manifest", "series"}`` where ``series``
    is ``None`` outside series mode.
    """
    expected = SERIES_MATCHES if series else 1
    if len(telemetry_docs) != expected or len(result_docs) != expected:
        raise ArtifactInputError(
            f"ARTIFACT_INPUT_COUNT_INVALID ({len(telemetry_docs)} telemetry / "
            f"{len(result_docs)} result documents; {expected} expected)")
    if fps < 24:
        raise ArtifactInputError("VIDEO_INPUT_INVALID")

    conversions = [convert(telemetry, result)
                   for telemetry, result in zip(telemetry_docs, result_docs)]
    replays = [canonical_replay.to_dict() for _evidence, canonical_replay in conversions]

    model_metadata = {
        IDENTITY_A: {"producer": "Poolside", "architecture": "Mixture-of-Experts", "context_length": 128_000},
        IDENTITY_B: {"producer": "Meta", "architecture": "Mixture-of-Experts", "context_length": 128_000},
    }
    benchmark_snapshot = load_benchmark_snapshot(Path(benchmark_data), IDENTITY_A, IDENTITY_B)
    print(f"benchmarks: {len(benchmark_snapshot)} dual-published rows "
          f"from {benchmark_data}", file=sys.stderr)

    summary = None
    if series:
        match_ids = [str(result.get("match_id") or "") for result in result_docs]
        # 'ep-v8-m1/-m2/-m3' -> series record 'ep-v8.series.json'.
        series_id = _derive_series_id([match_id for match_id in match_ids if match_id])
        summary = build_series_summary(series_id, list(result_docs), match_ids=match_ids)
        replay = merge_series_replays(replays)
        report = build_series_report(summary, list(result_docs))
    else:
        replay = replays[0]
        report = build_report(result_docs[0])

    # Arena choreography.  This is kept deterministic on purpose: the
    # renderer reproduces the same geometry/timing from the same replay, so
    # the rehearsal drafter (no model) is the right tool even when a text
    # model is available for the other phases.  It maps every Blue
    # hardening event to a defense artifact and every Red event to an attack
    # beat.
    #
    # Series note: ``arena_visuals`` is a manifest-global section, so one plan
    # is drafted over the MERGED replay rather than scoped to the last match.
    # Every beat stays grounded to red-phase event ids, and Remotion resolves
    # beat timing through the terminal feed (`ev.at_frame - localBase`), so
    # each beat lands inside whichever match scene owns its events.  The
    # win-probability bar therefore accumulates momentum across the series.
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
                                       drafter=intro_drafter or HeadlessIntroCommentaryDrafter())
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

    identities = [str(pane["identity"]) for pane in replay["panes"]]
    if series:
        # One dense deterministic rundown per match, concatenated: offsets are
        # re-anchored inside each match's own scenes by build_video_manifest.
        global_base_ns = int(replay["frames"][0]["at_monotonic_ns"])
        per_match_lines = [
            _dense_commentary_for_match(match_replay, position, identities, fps, global_base_ns)
            for position, match_replay in enumerate(replays, start=1)
        ]
        dense_commentary = [line for lines in per_match_lines for line in lines]
        print(f"commentary: deterministic dense rundown ({len(dense_commentary)} lines)",
              file=sys.stderr)
    else:
        # Dense two-voice commentary derived straight from the replay frames:
        # every terminal action (inspect, deploy, promote, verify, probe,
        # capture attempt, phase transition) is narrated at its own timestamp,
        # dead air is filled with recaps of seen actions, and nothing is
        # invented (v7 postmortem: the drafted path produced 11 lines for 26
        # events and skipped the blue phase).
        dense_commentary = build_commentary(replay.get("frames", []), identities, fps=fps)

    manifest = build_video_manifest(
        replay,
        report=report,
        model_metadata=model_metadata,
        benchmark_snapshot=benchmark_snapshot,
        fps=fps,
        arena_visuals=arena,
        intro_commentary=intro,
        dense_commentary=dense_commentary,
    )
    if dense_commentary and manifest.get("scenes"):
        # Nothing may speak inside or after the closing recap scene: clamp
        # windows to the recap boundary (episode-v8d: a failed match's
        # teardown event overflowed into factual_recap and tripped the TTS
        # COMMENTARY_OVERFLOW_AFTER_RENDER guardrail).  Applies to both the
        # per-match series path and the single-match path.
        boundary = sum(int(scene["duration_frames"]) for scene in manifest["scenes"][:-1])
        dense_commentary = [
            line for line in dense_commentary if int(line["start_frame"]) < boundary]
        for line in dense_commentary:
            start = int(line["start_frame"])
            line["end_frame"] = min(int(line["end_frame"]), max(boundary - 1, start + 1))
        if manifest.get("commentary"):
            manifest["commentary"] = [
                line for line in manifest["commentary"] if int(line["start_frame"]) < boundary]
            for line in manifest["commentary"]:
                start = int(line["start_frame"])
                line["end_frame"] = min(int(line["end_frame"]), max(boundary - 1, start + 1))
    return {"replay": replay, "report": report, "manifest": manifest, "series": summary}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--telemetry", type=Path, nargs="+", required=True,
                        help="match telemetry JSONL (run_command_code_match output); "
                             "three files in series mode")
    parser.add_argument("--result", type=Path, nargs="+", required=True,
                        help="match result.json (run_command_code_match output); "
                             "three files in series mode")
    parser.add_argument("--series", action="store_true",
                        help="treat inputs as one best-of-3 episode and emit combined artifacts")
    parser.add_argument("--require-valid", type=int, default=2, metavar="N",
                        help="series mode only: refuse to write artifacts unless at least N "
                             "matches recorded result=passed with outcome VALID_CAPTURE "
                             "(default: 2); bypass with --force")
    parser.add_argument("--force", action="store_true",
                        help="series mode only: write artifacts even when fewer than "
                             "--require-valid matches are valid")
    parser.add_argument("--series-id", default=None,
                        help="series identifier for <series-id>.series.json; "
                             "defaults to the first match id without its -m<i> suffix")
    parser.add_argument("--out-dir", type=Path, default=ROOT)
    parser.add_argument("--run-id", default=None,
                        help="isolate outputs under <artifacts-root>/runs/<run-id>/ (sandboxer_v0.run_layout)")
    parser.add_argument("--artifacts-root", type=Path, default=None,
                        help="artifacts root for --run-id; defaults to --out-dir")
    parser.add_argument("--benchmark-data", type=Path, default=DEFAULT_BENCHMARK_DATA,
                        help=f"benchmark snapshot dataset JSON (default: {DEFAULT_BENCHMARK_DATA})")
    args = parser.parse_args(argv)

    if len(args.telemetry) != len(args.result):
        print(f"TELEMETRY_RESULT_COUNT_MISMATCH ({len(args.telemetry)} telemetry vs "
              f"{len(args.result)} result files)", file=sys.stderr)
        return 2
    expected = SERIES_MATCHES if args.series else 1
    if len(args.telemetry) != expected:
        print(f"ARTIFACT_INPUT_COUNT_INVALID ({len(args.telemetry)} matches; "
              f"{expected} expected for {'--series' if args.series else 'single-match'} mode)",
              file=sys.stderr)
        return 2

    layout = None
    out_dir = args.out_dir
    if args.run_id is not None:
        layout = get_run_layout(args.artifacts_root or args.out_dir, args.run_id)
        out_dir = layout.directory

    def read_jsonl(path: Path) -> list[dict]:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    try:
        telemetry_docs = [read_jsonl(path) for path in args.telemetry]
        result_docs = [json.loads(path.read_text(encoding="utf-8")) for path in args.result]
    except (OSError, json.JSONDecodeError) as error:
        print(f"ARTIFACT_INPUT_UNREADABLE ({error})", file=sys.stderr)
        return 2
    if args.series and not args.force:
        valid = count_valid_capture_results(result_docs)
        if valid < args.require_valid:
            print(f"SERIES_INSUFFICIENT_VALID_MATCHES ({valid} of {len(result_docs)} matches "
                  f"passed with VALID_CAPTURE; {args.require_valid} required; "
                  f"pass --force to write artifacts anyway)", file=sys.stderr)
            return 2
    try:
        artifacts = build_broadcast_artifacts(
            telemetry_docs, result_docs,
            benchmark_data=args.benchmark_data,
            series=args.series,
        )
    except (OSError, json.JSONDecodeError) as error:
        print(f"ARTIFACT_INPUT_UNREADABLE ({error})", file=sys.stderr)
        return 2
    replay = artifacts["replay"]
    report = artifacts["report"]
    manifest = artifacts["manifest"]

    # The manifest's own terminal feed is already scene-aligned (each frame
    # carries the absolute at_frame of its owning scene segment), so no
    # renderer-side recomputation happens here and the manifest hash recorded
    # inside build_video_manifest stays authoritative.
    out_dir.mkdir(parents=True, exist_ok=True)
    replay_path = run_artifact_path(out_dir, "replay") if layout else out_dir / "replay.json"
    report_path = run_artifact_path(out_dir, "report") if layout else out_dir / "report.json"
    vmanifest_path = run_artifact_path(out_dir, "video_manifest") if layout else out_dir / "video-manifest.json"
    replay_path.write_text(json.dumps(replay, indent=2), encoding="utf-8")
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    vmanifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (out_dir / "remotion-props.json").write_text(
        json.dumps({"manifest": manifest}, indent=2), encoding="utf-8")
    if artifacts["series"] is not None:
        summary = dict(artifacts["series"])
        if args.series_id:
            summary["series_id"] = args.series_id
        (out_dir / f"{summary['series_id']}.series.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote series record: {summary['series_id']}.series.json "
              f"(winner: {summary.get('series_winner') or 'unresolved'}, "
              f"basis: {summary.get('decision_basis')})")
    if layout is not None:
        if args.series:
            inputs = {
                **{f"telemetry_{index}": path for index, path in enumerate(args.telemetry, start=1)},
                **{f"result_{index}": path for index, path in enumerate(args.result, start=1)},
            }
        else:
            inputs = {"telemetry": args.telemetry[0], "result": args.result[0]}
        write_run_manifest(out_dir, args.run_id, inputs)
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