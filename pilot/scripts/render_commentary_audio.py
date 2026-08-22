"""Render TTS commentary and assemble the full audio track.

This is the canonical entry point for the broadcast TTS step.  It reads
``artifacts/video-manifest.json``, synthesizes every commentary line through a
provider adapter (Gemini by default; Fish Audio via ``--provider fish``), with
resume of already-rendered blocks, then places each block into a full-length
24 kHz mono track at its ``start_frame`` for muxing with the Remotion video.

Outputs:
    artifacts/broadcast.audio/block-XXXX.wav   per-line bounded blocks
    artifacts/broadcast.audio/block-XXXX.model sidecar (model that rendered)
    artifacts/commentary-full.wav              full-length aligned track

Gemini usage:
    GEMINI_API_KEY=... SANDBOXER_TTS_ALLOW_FALLBACK=1 \\
        python scripts/render_commentary_audio.py [--out-dir DIR] [--no-resume]

The Gemini fallback chain is configurable via GEMINI_TTS_FALLBACK_MODELS
(comma-separated, in priority order) and defaults to the pinned primary plus
Gemini 2.5 Flash preview TTS.

Fish Audio usage (free ``s2.1-pro-free`` developer tier):
    FISH_API_KEY=... FISH_TTS_VOICES="Kore=<ref>,Charon=<ref>" \\
        python scripts/render_commentary_audio.py --provider fish
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sandboxer_v0.tts import (
    DEFAULT_FISH_TTS_MODEL,
    DEFAULT_TTS_FALLBACK_MODELS,
    DEFAULT_TTS_MODEL,
    FishAudioTtsAdapter,
    GeminiTtsAdapter,
    parse_voice_spec,
    render_commentary_audio,
)
from sandboxer_v0.video import _digest, validate_provenance

ROOT = Path(__file__).resolve().parent.parent.parent / "artifacts"
RATE = 24_000
PACE_BETWEEN_BLOCKS = 8.0  # seconds between fresh renders (free-tier friendly)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=ROOT / "video-manifest.json")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "broadcast.audio")
    parser.add_argument("--no-resume", action="store_true", help="re-render every block")
    parser.add_argument("--track", type=Path, default=ROOT / "commentary-full.wav")
    parser.add_argument(
        "--provider",
        choices=("gemini", "fish"),
        default=os.environ.get("SANDBOXER_TTS_PROVIDER", "gemini"),
        help="TTS provider (default from SANDBOXER_TTS_PROVIDER, else gemini; "
        "fish uses FISH_API_KEY + FISH_TTS_VOICES)",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="override the primary TTS model (defaults to the manifest contract)",
    )
    parser.add_argument(
        "--no-probe",
        action="store_true",
        help="skip the preflight availability probe (finish a render already underway)",
    )
    parser.add_argument(
        "--chain-retries",
        type=int,
        default=40,
        help="whole-chain retry budget once every model is inside a quota window",
    )
    parser.add_argument(
        "--pacing",
        type=float,
        default=PACE_BETWEEN_BLOCKS,
        help="seconds between fresh block renders (free-tier rolling-window friendly)",
    )
    args = parser.parse_args(argv)

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    fps = manifest["fps"]
    total_frames = sum(scene["duration_frames"] for scene in manifest["scenes"])
    total_ms = round(total_frames / fps * 1000)

    if args.provider == "fish":
        fish_key = os.environ.get("FISH_API_KEY")
        if not fish_key:
            print("FISH_KEY_MISSING: set FISH_API_KEY", file=sys.stderr)
            return 2
        reference_ids = parse_voice_spec(os.environ.get("FISH_TTS_VOICES", ""))
        if not reference_ids:
            print('FISH_VOICES_MISSING: set FISH_TTS_VOICES="Kore=<ref>,Charon=<ref>"', file=sys.stderr)
            return 2
        model = args.model or os.environ.get("FISH_TTS_MODEL") or DEFAULT_FISH_TTS_MODEL
        adapter = FishAudioTtsAdapter(
            api_key=fish_key,
            model=model,
            reference_ids=reference_ids,
            retries=2,
            retry_base_seconds=5.0,
            probe=not args.no_probe,
        )
        print(f"adapter: fish primary={model} voices={sorted(reference_ids)}")
    else:
        key = os.environ.get("GEMINI_API_KEY")
        if not key:
            print("GEMINI_KEY_MISSING: set GEMINI_API_KEY", file=sys.stderr)
            return 2
        allow_fallback = os.environ.get("SANDBOXER_TTS_ALLOW_FALLBACK", "").strip().lower() in ("1", "true", "yes")
        fallback_env = os.environ.get("GEMINI_TTS_FALLBACK_MODELS", "").strip()
        fallback_models = tuple(m.strip() for m in fallback_env.split(",") if m.strip()) if fallback_env else DEFAULT_TTS_FALLBACK_MODELS
        model = args.model or manifest.get("tts", {}).get("expected", {}).get("model") or DEFAULT_TTS_MODEL

        adapter = GeminiTtsAdapter(
            api_key=key,
            model=model,
            fallback_models=fallback_models,
            allow_fallback=allow_fallback,
            retries=2,
            retry_base_seconds=5.0,
            chain_retries=args.chain_retries,
            probe=not args.no_probe,
        )
        print(f"adapter: gemini primary={model} fallback={list(fallback_models)} allow_fallback={allow_fallback}")

    section = render_commentary_audio(
        manifest,
        adapter,
        out_dir=args.out_dir,
        resume=not args.no_resume,
        pacing_seconds=args.pacing,
    )
    print(f"rendered {section['block_count']} blocks (hash {section['blocks_hash'][:12]}, "
          f"models_used={section['models_used']})")

    # Rewrite the manifest TTS section with the requested/observed provenance
    # pair plus the drift flag, so the artifact records which provider really
    # produced the audio (not just what was pinned before synthesis).
    manifest["tts"]["requested"] = section["requested"]
    manifest["tts"]["observed"] = section["observed"]
    manifest["tts"]["tts_provider_drift"] = section["tts_provider_drift"]
    problems = validate_provenance(manifest)
    if problems:
        print(f"TTS_PROVENANCE_INVALID: {', '.join(problems)}", file=sys.stderr)
        return 2
    print(f"tts provenance: requested={section['requested']['provider']}/{section['requested']['model']} "
          f"observed={section['observed']['provider']}/{section['observed']['model']} "
          f"drift={section['tts_provider_drift']}")

    # Persist the actual-duration-packed schedule back into the manifest so
    # captions and the Remotion caption bar stay in sync with the audio.
    for line, block in zip(manifest["commentary"], section["blocks"]):
        line["start_frame"] = block["start_frame"]
        line["end_frame"] = block["end_frame"]
    manifest["manifest_hash"] = _digest(manifest)
    args.manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    print(f"updated {args.manifest} with packed commentary schedule")

    # Assemble the full-length track with each block at its start_frame.
    track = bytearray(total_ms * RATE // 1000 * 2)
    for block in section["blocks"]:
        with wave.open(str(args.out_dir / block["file"]), "rb") as reader:
            data = reader.readframes(reader.getnframes())
        at = block["start_frame"] * RATE // fps
        end = min(len(track) // 2, at + len(data) // 2)
        track[at * 2 : end * 2] = data[: (end - at) * 2]
    args.track.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(args.track), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(RATE)
        writer.writeframes(bytes(track))
    print(f"wrote {args.track} ({total_ms / 1000:.1f}s track)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
