"""Join per-line TTS blocks into a single full-length voice track.

render_commentary_audio() writes one WAV per commentary line (block-NNNN.wav
keyed by manifest order) but publish_broadcast.py expects a single
commentary-full.wav beside the audio dir for the BGM underlay step.

This module places every block at its scheduled start_sample
(= round(start_frame / fps * sample_rate)) inside a silent buffer sized to
the whole video, so commentary-full.wav covers the entire timeline. Blocks
that would run past the next block's start (overlap) or past the video end are
clipped (truncated) to silence rather than mixed.
"""
from __future__ import annotations

import argparse
from collections.abc import Mapping
import json
import wave
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

TOTAL_FRAMES = 35940
FPS = 30
SAMPLE_RATE = 24000
TARGET_SAMPLES = round(TOTAL_FRAMES / FPS * SAMPLE_RATE)  # 28_752_000


def _read_all_frames(w: wave.Wave_read) -> bytes:
    """Read every PCM frame, robust to a bogus/overflowing nframes header."""
    chunks = []
    while True:
        chunk = w.readframes(4096)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def assemble_voice_track(
    manifest: Mapping[str, Any],
    audio_dir: Path,
    out_path: Path,
    total_frames: int | None = None,
) -> tuple[int, int, int]:
    """Assemble a full-length 24kHz mono voice track from individual TTS blocks.

    Returns (target_samples, placed_count, clipped_count).
    """
    fps = int(manifest.get("fps", FPS))
    if total_frames is None:
        scenes = manifest.get("scenes", [])
        if scenes:
            total_frames = sum(int(s.get("duration_frames", 0)) for s in scenes)
        else:
            total_frames = TOTAL_FRAMES
    target_samples = round(total_frames / fps * SAMPLE_RATE)

    # Prefer tts.blocks (actual duration packed schedule) if available, else commentary
    tts_blocks = manifest.get("tts", {}).get("blocks") if isinstance(manifest.get("tts"), Mapping) else None
    if tts_blocks and isinstance(tts_blocks, list):
        block_items = []
        for b in tts_blocks:
            fname = b.get("file") or f"block-{len(block_items):04d}.wav"
            block_items.append((audio_dir / fname, int(b.get("start_frame", 0))))
    else:
        lines = manifest.get("commentary", [])
        block_items = [
            (audio_dir / f"block-{idx:04d}.wav", int(line.get("start_frame", 0)))
            for idx, line in enumerate(lines)
        ]

    missing = [str(p) for p, _ in block_items if not p.is_file()]
    if missing:
        raise FileNotFoundError(f"Missing audio blocks: {missing[:5]} (total {len(missing)})")

    scheduled = []
    params = None
    for p, start_frame in block_items:
        with wave.open(str(p), "rb") as w:
            if params is None:
                params = (w.getnchannels(), w.getsampwidth(), w.getframerate())
            else:
                assert params == (w.getnchannels(), w.getsampwidth(), w.getframerate()),                     f"format mismatch in {p}"
            pcm = _read_all_frames(w)
        start_sample = round(start_frame / fps * SAMPLE_RATE)
        scheduled.append((start_sample, pcm))

    if not scheduled:
        n_channels, sampwidth, rate = 1, 2, SAMPLE_RATE
    else:
        n_channels, sampwidth, rate = params
        assert sampwidth == 2, "expected 16-bit PCM"
        assert rate == SAMPLE_RATE, f"expected {SAMPLE_RATE} Hz, got {rate}"

    scheduled.sort(key=lambda s: s[0])
    frame_bytes = sampwidth * n_channels
    target_bytes = target_samples * frame_bytes
    buf = bytearray(target_bytes)

    placed = 0
    clipped = 0
    for i, (start_sample, pcm) in enumerate(scheduled):
        start_byte = start_sample * frame_bytes
        if start_byte >= target_bytes:
            clipped += 1
            continue
        next_start = scheduled[i + 1][0] if i + 1 < len(scheduled) else None
        room = target_bytes - start_byte
        if next_start is not None and next_start > start_sample:
            room = min(room, (next_start - start_sample) * frame_bytes)
        write_len = min(len(pcm), room)
        if write_len <= 0:
            clipped += 1
            continue
        buf[start_byte : start_byte + write_len] = pcm[:write_len]
        placed += 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out_path), "wb") as w:
        w.setnchannels(n_channels)
        w.setsampwidth(sampwidth)
        w.setframerate(rate)
        w.writeframes(bytes(buf))

    return target_samples, placed, clipped


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--total-frames", type=int, default=None,
                        help="total video frames; default derived from manifest scenes")
    args = parser.parse_args(argv)

    run_dir = args.run_dir
    manifest = json.loads((run_dir / "video-manifest.json").read_text(encoding="utf-8"))

    audio_dir = run_dir / "video-manifest.audio"
    if not audio_dir.is_dir():
        audio_dir = run_dir / "broadcast-record.audio"
    if not audio_dir.is_dir():
        audio_dir = run_dir.parent / f"{run_dir.name}.audio"
    if not audio_dir.is_dir():
        print(f"audio dir not found (tried {audio_dir})")
        return 2

    out = run_dir / "commentary-full.wav"
    target_samples, placed, clipped = assemble_voice_track(
        manifest=manifest,
        audio_dir=audio_dir,
        out_path=out,
        total_frames=args.total_frames,
    )
    print(
        f"wrote {out} ({target_samples * 2} bytes, {target_samples} samples @ {SAMPLE_RATE} Hz, "
        f"{target_samples / SAMPLE_RATE:.3f} s, {placed} blocks placed, {clipped} clipped)",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
