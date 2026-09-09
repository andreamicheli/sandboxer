"""Tests for joining TTS blocks into a full-length scheduled voice track."""
from pathlib import Path
import json
import wave
import struct

from scripts.join_voice_track import assemble_voice_track, SAMPLE_RATE, _read_all_frames


def _create_sine_wav(path: Path, duration_seconds: float, freq: float = 440.0, rate: int = SAMPLE_RATE) -> None:
    """Write a simple 16-bit mono PCM wav."""
    import math
    num_samples = int(duration_seconds * rate)
    data = bytearray()
    for i in range(num_samples):
        val = int(16000 * math.sin(2 * math.pi * freq * i / rate))
        data.extend(struct.pack("<h", val))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(data))


def test_assemble_voice_track_spans_entire_video_with_scheduled_padding(tmp_path: Path):
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()

    # Create 3 audio blocks: block 0 at 0s, block 1 at 300s (frame 9000), block 2 at 700s (frame 21000)
    # Total video length is 1000s (30000 frames at 30 fps)
    _create_sine_wav(audio_dir / "block-0000.wav", duration_seconds=2.0)
    _create_sine_wav(audio_dir / "block-0001.wav", duration_seconds=3.0)
    _create_sine_wav(audio_dir / "block-0002.wav", duration_seconds=2.5)

    manifest = {
        "fps": 30,
        "scenes": [
            {"type": "intro", "duration_frames": 9000},    # 300s
            {"type": "match", "duration_frames": 12000},   # 400s
            {"type": "recap", "duration_frames": 9000},    # 300s -> total 1000s = 30000 frames
        ],
        "commentary": [
            {"start_frame": 0, "text": "Start line"},
            {"start_frame": 9000, "text": "Middle line at 300s"},
            {"start_frame": 21000, "text": "Late line at 700s"},
        ],
    }

    out_track = tmp_path / "commentary-full.wav"
    target_samples, placed, clipped = assemble_voice_track(manifest, audio_dir, out_track)

    assert placed == 3
    assert clipped == 0
    assert target_samples == 30000 * SAMPLE_RATE // 30  # 24,000,000 samples = 1000s

    # Verify generated WAV file properties
    with wave.open(str(out_track), "rb") as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getframerate() == SAMPLE_RATE
        frames = _read_all_frames(w)
        total_pcm_samples = len(frames) // 2
        assert total_pcm_samples == target_samples

    # Check that there is sound (non-zero PCM) around 700s (sample 700 * 24000 = 16,800,000)
    late_offset = 700 * SAMPLE_RATE * 2
    late_chunk = frames[late_offset : late_offset + 24000 * 2]
    # Should not be all zeros
    assert any(b != 0 for b in late_chunk), "Expected audio at 700s, but found silence!"

    # Check that there is silence between block 1 and block 2 (e.g. at 500s = 12,000,000 samples)
    mid_offset = 500 * SAMPLE_RATE * 2
    mid_chunk = frames[mid_offset : mid_offset + 1000 * 2]
    assert all(b == 0 for b in mid_chunk), "Expected silence at 500s, but found non-zero samples!"
