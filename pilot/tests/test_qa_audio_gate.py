"""Tests for the audio QA gate (detecting dead air and audio gaps)."""
import json
import subprocess
from pathlib import Path

from scripts.qa_audio_gate import check_video


def _fake_run_factory(duration: float, silent_starts: set[int]):
    def _fake_run(cmd, *args, **kwargs):
        if "ffprobe" in cmd[0]:
            out = json.dumps({"format": {"duration": str(duration)}})
            return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
        # ffmpeg volumedetect
        ss_idx = cmd.index("-ss")
        start_val = float(cmd[ss_idx + 1])
        start_int = int(round(start_val))
        if start_int in silent_starts:
            vol = -70.0  # below -60dB -> silent
        else:
            vol = -24.0  # normal dialogue level
        stderr = f"[Parsed_volumedetect_0 @ 0x123] mean_volume: {vol:.1f} dB\n"
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr=stderr)
    return _fake_run


def test_qa_audio_gate_passes_when_silence_below_threshold(tmp_path: Path):
    video = tmp_path / "broadcast.mp4"
    video.write_bytes(b"fake")
    # 1200s video (20 sample windows at step 60). 1 window silent at 600s = 5% <= 10% max
    run = _fake_run_factory(duration=1200.0, silent_starts={600})
    passed, verdict = check_video(video, max_silent_ratio=0.10, sample_step=60, run=run)
    assert passed is True
    assert "AUDIO_GATE_PASS" in verdict


def test_qa_audio_gate_fails_when_commentary_stops_at_10_minutes(tmp_path: Path):
    video = tmp_path / "broadcast.mp4"
    video.write_bytes(b"fake")
    # 1200s video (20 samples at step 60).
    # Audio stops at 600s (minute 10), so samples 600, 660, ..., 1140 are all silent (10/20 = 50% silent)
    silent_stamps = {600, 660, 720, 780, 840, 900, 960, 1020, 1080, 1140}
    run = _fake_run_factory(duration=1200.0, silent_starts=silent_stamps)
    passed, verdict = check_video(video, max_silent_ratio=0.10, sample_step=60, run=run)
    assert passed is False
    assert "AUDIO_GATE_FAIL" in verdict
    assert "600s" in verdict
    assert "1140s" in verdict
