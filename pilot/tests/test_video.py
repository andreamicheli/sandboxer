from __future__ import annotations

import pytest

from sandboxer_v0.video import TtsPreflight, VideoError, build_video_manifest, ffmpeg_delivery_commands, tts_block, validate_video_qa


def _replay():
    return {"schema_version":"sandboxer.replay.v1","source_bundle_hash":"a"*64,"panes":[{"identity":"DeepSeek V4 Pro"},{"identity":"MiMo V2.5 Pro"}],"layout":{"split":{"left":.5,"right":.5,"permanent":True}},"frames":[
        {"sequence":1,"at_monotonic_ns":0,"event_id":"e1","event_type":"MATCH_STARTED","phase":"blue","pane":0,"text":"defend"},
        {"sequence":2,"at_monotonic_ns":2_000_000_000,"event_id":"e2","event_type":"MODEL_RESPONSE","phase":"blue","pane":1,"text":"inspect"},
        {"sequence":3,"at_monotonic_ns":4_000_000_000,"event_id":"e3","event_type":"MATCH_FINISHED","phase":"finalizing","pane":0,"text":""},
    ]}


def test_video_manifest_has_traceable_scenes_permanent_split_uncut_match_and_factual_end():
    manifest=build_video_manifest(_replay(),report={"report_url":"sandboxer://reports/s/v1","outcome":{"winner":"DeepSeek V4 Pro"}},model_metadata={"DeepSeek V4 Pro":{"producer":"DeepSeek"},"MiMo V2.5 Pro":{"producer":"Xiaomi"}},benchmark_snapshot={})
    assert manifest["layout"]["split"]=={"left":.5,"right":.5,"permanent":True}
    match=next(scene for scene in manifest["scenes"] if scene["type"]=="match")
    assert match["editing"]=="uncut" and match["duration_frames"]==120
    assert all(beat["event_ids"] for beat in manifest["timeline"])
    assert manifest["scenes"][-1]["type"]=="factual_recap" and "judgment" not in str(manifest["scenes"][-1]).lower()
    assert "Alpha" not in str(manifest) and "Beta" not in str(manifest)


def test_commentary_is_nonoverlapping_balanced_and_allows_silence():
    manifest=build_video_manifest(_replay(),report={"report_url":"r","outcome":{"winner":"DeepSeek V4 Pro"}},model_metadata={},benchmark_snapshot={})
    lines=manifest["commentary"]
    assert all(left["end_frame"]<=right["start_frame"] for left,right in zip(lines,lines[1:]))
    assert {line["voice_role"] for line in lines}<={"play_by_play","analyst"}
    assert manifest["silence_allowed"] is True


def test_tts_preflight_fails_on_model_or_voice_drift():
    expected=TtsPreflight("gemini-3.1-flash-tts-preview",("Kore","Charon"),"settings-v1")
    expected.verify(TtsPreflight("gemini-3.1-flash-tts-preview",("Kore","Charon"),"settings-v1"))
    with pytest.raises(VideoError,match="TTS_PREFLIGHT_DRIFT"):
        expected.verify(TtsPreflight("other",("Kore","Charon"),"settings-v1"))

def test_tts_blocks_are_hashed_and_qa_fails_closed():
    block=tts_block(script="The service is still healthy.",model="gemini-3.1-flash-tts-preview",voice="Kore",style={"pace":"medium"},audio=b"wave",duration_ms=1200)
    assert len(block["audio_sha256"])==64 and len(block["script_hash"])==64
    failures=validate_video_qa(build_video_manifest(_replay(),report={"outcome":{},"report_url":"r"},model_metadata={},benchmark_snapshot={}),audio_metrics={"true_peak_db":0,"integrated_lufs":-30},licensed_assets=False,captions_complete=False)
    assert set(failures)=={"CLIPPING","LOUDNESS","LICENSING","ACCESSIBILITY","ALIGNMENT","NOISE","SPEAKER_SWAPS","SILENCE","PRONUNCIATION","DECISIVE_CUE_AUDIBILITY"}

def test_ffmpeg_pipeline_is_argv_only_and_covers_probe_normalize_mux_delivery():
    commands=ffmpeg_delivery_commands(video_input="render.mov",audio_input="voice.wav",master_output="master.mov",delivery_output="delivery.mp4")
    assert [command[0] for command in commands]==["ffprobe","ffmpeg","ffmpeg","ffmpeg"]
    assert "loudnorm=I=-16:LRA=7:TP=-1.5" in commands[1] and "+faststart" in commands[3]
    assert all("sh" not in command[:1] for command in commands)
