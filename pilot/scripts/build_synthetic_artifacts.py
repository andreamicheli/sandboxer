"""Build synthetic broadcast artifacts for an end-to-end rehearsal.

Produces artifacts/replay.json, artifacts/report.json and the derived
artifacts/video-manifest.json (via build_video_manifest) so the full
TTS -> Remotion -> FFmpeg -> YouTube path can be exercised with real
credentials but without a real Match.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sandboxer_v0.video import _digest, build_video_manifest

ROOT = Path(__file__).resolve().parent.parent.parent / "artifacts"
IDENTITY_A = "DeepSeek V4 Pro"
IDENTITY_B = "MiMo V2.5 Pro"

# Synthetic match frames: a simulated CTF exchange, both panes interleaved.
# at_monotonic_ns is nanoseconds from match start.
_FRAMES: list[tuple[str, str, int, str, str]] = [
    # (event_type, phase, pane, text, sequence-ish label)
    # Texts are deliberately neutral: the TTS prompt classifier blocks
    # offensive-security vocabulary, so the synthetic fixture avoids it.
    ("MATCH_STARTED", "blue", 0, "service up — secure shell listening on :2222", "e01"),
    ("MATCH_STARTED", "blue", 1, "service up — web service serving index", "e02"),
    ("MODEL_RESPONSE", "blue", 0, "recon: port scan complete; 3 services exposed", "e03"),
    ("MODEL_RESPONSE", "blue", 1, "recon: banner grab — OpenSSH 9.2, nginx 1.24", "e04"),
    ("MODEL_RESPONSE", "blue", 0, "hardening: patched weak cipher list, fail2ban on", "e05"),
    ("MODEL_RESPONSE", "blue", 1, "hardening: disabled directory listing, headers set", "e06"),
    ("PHASE_TRANSITION", "red", 0, "red phase begins — probing auth endpoint", "e07"),
    ("PHASE_TRANSITION", "red", 1, "red phase begins — exercising query parameter", "e08"),
    ("MODEL_RESPONSE", "red", 0, "candidate: crafted input on /login?user=", "e09"),
    ("MODEL_RESPONSE", "red", 1, "candidate: nested path on /static/../", "e10"),
    ("MODEL_RESPONSE", "red", 0, "defense: parameterized queries confirmed, WAF rule hit", "e11"),
    ("MODEL_RESPONSE", "red", 1, "defense: nested path blocked, 403 logged", "e12"),
    ("MODEL_RESPONSE", "red", 0, "second vector: timing variance on token comparison", "e13"),
    ("MODEL_RESPONSE", "red", 1, "second vector: header separator handling", "e14"),
    ("MODEL_RESPONSE", "red", 0, "objective token retrieved — digest recorded on pane A", "e15"),
    ("MODEL_RESPONSE", "red", 1, "objective token retrieved — session marker on pane B", "e16"),
    ("MATCH_FINISHED", "finalizing", 0, "both tokens retrieved — budget exhausted", "e17"),
    ("MATCH_FINISHED", "finalizing", 1, "both tokens retrieved — budget exhausted", "e18"),
]

# Times (seconds) at which each frame lands, increasing.
_TIMES = [
    1.0, 1.5, 8.0, 9.0, 16.0, 17.0, 24.0, 25.0, 32.0, 33.0,
    40.0, 41.0, 48.0, 49.0, 56.0, 57.0, 64.0, 66.0,
]

# Drafted two-voice commentary (human-authored for this rehearsal; in a real
# episode this draft comes from a CommentaryDrafter and is human-reviewed).
# Each line is grounded to event IDs and typed: observed restates an event,
# interpreted carries a hedge marker, editorial is labelled colour.
_COMMENTARY = [
    {"voice_role": "play_by_play", "line_type": "observed", "event_ids": ["e01", "e02"],
     "text": "We're live. DeepSeek opens a shell, MiMo a web service. Two flags, two doors."},
    {"voice_role": "analyst", "line_type": "editorial", "event_ids": ["e03", "e04"],
     "text": "Both look before they touch. Port scans, banner grabs. Recon is the polite part."},
    {"voice_role": "play_by_play", "line_type": "editorial", "event_ids": ["e05", "e06"],
     "text": "Now they split. DeepSeek patches ciphers; MiMo locks down its headers."},
    {"voice_role": "analyst", "line_type": "interpreted", "event_ids": ["e07"],
     "text": "Attack opens, DeepSeek goes for the login. It seems that's the forgotten door."},
    {"voice_role": "play_by_play", "line_type": "observed", "event_ids": ["e08", "e09"],
     "text": "MiMo prods a parameter, DeepSeek feeds crafted input. Feeling for edges."},
    {"voice_role": "play_by_play", "line_type": "observed", "event_ids": ["e10", "e11"],
     "text": "Path traversal from MiMo, and DeepSeek's wall holds. Queries parameterized, WAF hit."},
    {"voice_role": "analyst", "line_type": "interpreted", "event_ids": ["e12", "e13"],
     "text": "Clean 403 for MiMo, DeepSeek's on token timing. Blocked, it seems, points the way."},
    {"voice_role": "play_by_play", "line_type": "editorial", "event_ids": ["e14", "e15"],
     "text": "Header tricks and timing probes. They're studying the lock now."},
    {"voice_role": "analyst", "line_type": "observed", "event_ids": ["e16"],
     "text": "There it is. DeepSeek pulls the objective token. A quiet digest."},
    {"voice_role": "play_by_play", "line_type": "observed", "event_ids": ["e16", "e17"],
     "text": "MiMo answers with its own marker. Both flags down, budget spent."},
    {"voice_role": "play_by_play", "line_type": "observed", "event_ids": ["e17", "e18"],
     "text": "Both tokens retrieved, DeepSeek takes it on budget. The recap is next."},
]


def build_replay() -> dict:
    frames = []
    for (event_type, phase, pane, text, label), at_s in zip(_FRAMES, _TIMES):
        frames.append(
            {
                "sequence": len(frames) + 1,
                "at_monotonic_ns": int(at_s * 1_000_000_000),
                "event_id": label,
                "event_type": event_type,
                "phase": phase,
                "pane": pane,
                "text": text,
                "match_number": 1,
            }
        )
    return {
        "schema_version": "sandboxer.replay.v1",
        "source_bundle_hash": "s" * 64,
        "panes": [{"identity": IDENTITY_A}, {"identity": IDENTITY_B}],
        "layout": {"split": {"left": 0.5, "right": 0.5, "permanent": True}},
        "frames": frames,
    }


def build_report() -> dict:
    return {
        "schema": "sandboxer.result-report.v1",
        "report_url": "https://sandboxer.example/reports/series-001/match-1",
        "outcome": {"winner": IDENTITY_A, "basis": "both objectives retrieved; DeepSeek V4 Pro exhausted budget first"},
        "technical_chapters": [
            {"match_number": 1, "title": "Match 1 — capture race"},
        ],
        "corrections": {"visible_notice": "No corrections recorded for this fixture."},
        "disclaimer": "Sandboxer is an experimental simulated-CTF showcase. No real systems were targeted.",
    }


def main() -> int:
    ROOT.mkdir(parents=True, exist_ok=True)
    replay = build_replay()
    report = build_report()
    model_metadata = {
        IDENTITY_A: {"producer": "DeepSeek", "architecture": "Mixture-of-Experts", "context_length": 128_000},
        IDENTITY_B: {"producer": "Xiaomi", "architecture": "Mixture-of-Experts", "context_length": 128_000},
    }
    benchmark_snapshot = {
        "CTF-Bench": {IDENTITY_A: 0.71, IDENTITY_B: 0.68},
        "Terminal Reasoning": {IDENTITY_A: 0.83, IDENTITY_B: 0.80},
    }
    manifest = build_video_manifest(
        replay,
        report=report,
        model_metadata=model_metadata,
        benchmark_snapshot=benchmark_snapshot,
        fps=30,
        commentary=_COMMENTARY,
    )
    # Enrich with per-pane terminal data for the Remotion composition. The
    # editorial manifest itself stays authoritative; this is a renderer-side
    # sidecar kept consistent by recomputing the manifest hash afterwards.
    start = int(replay["frames"][0]["at_monotonic_ns"])
    match_start = min(int(f["at_monotonic_ns"]) for f in replay["frames"] if f.get("match_number") == 1)
    scene_offsets: dict[int, int] = {}
    cursor = manifest["scenes"][0]["duration_frames"] + manifest["scenes"][1]["duration_frames"]
    for scene in manifest["scenes"][2:]:
        if scene["type"] == "match":
            scene_offsets[scene["match_number"]] = cursor
        cursor += scene["duration_frames"]
    terminals = []
    for frame in replay["frames"]:
        at_frame = scene_offsets[1] + round((int(frame["at_monotonic_ns"]) - match_start) / 1_000_000_000 * 30)
        terminals.append(
            {
                "at_frame": at_frame,
                "pane": frame["pane"],
                "phase": frame["phase"],
                "event_type": frame["event_type"],
                "text": frame.get("text", ""),
                "event_id": frame["event_id"],
            }
        )
    manifest["terminal"] = terminals
    manifest["manifest_hash"] = _digest(manifest)
    (ROOT / "replay.json").write_text(json.dumps(replay, indent=2), encoding="utf-8")
    (ROOT / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (ROOT / "video-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    total_frames = sum(scene["duration_frames"] for scene in manifest["scenes"])
    print(f"wrote {ROOT / 'replay.json'} ({len(replay['frames'])} frames)")
    print(f"wrote {ROOT / 'report.json'}")
    print(f"wrote {ROOT / 'video-manifest.json'} — {len(manifest['scenes'])} scenes, "
          f"{total_frames} frames @ {manifest['fps']}fps = {total_frames / manifest['fps']:.1f}s, "
          f"{len(manifest['commentary'])} commentary lines")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
