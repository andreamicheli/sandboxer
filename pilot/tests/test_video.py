from __future__ import annotations

import pytest

from sandboxer_v0.video import TtsPreflight, VideoError, _scene_budgets, build_video_manifest, ffmpeg_delivery_commands, tts_block, validate_video_qa


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


def test_video_manifest_exposes_per_pane_terminal_feed():
    manifest=build_video_manifest(_replay(),report={"report_url":"r","outcome":{"winner":"DeepSeek V4 Pro"}},model_metadata={},benchmark_snapshot={})
    terminal=manifest["terminal"]
    assert len(terminal)==3
    assert terminal[0]["pane"]==0 and terminal[1]["pane"]==1
    assert terminal[0]["text"]=="defend" and terminal[0]["event_id"]=="e1" and terminal[1]["phase"]=="blue"
    # Terminal frames are scene-aligned: e1 sits at the match scene start
    # (8s custom intro + 20s model cards = 28s) so the renderer's localBase
    # window filtering selects exactly each scene's own events.
    match_start=manifest["scenes"][0]["duration_frames"]+manifest["scenes"][1]["duration_frames"]
    assert terminal[0]["at_frame"]==match_start and terminal[1]["at_frame"]==match_start+60


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


def test_video_manifest_schedules_drafted_commentary():
    drafted=[
        {"voice_role":"play_by_play","line_type":"observed","event_ids":["e1"],"text":"DeepSeek defends."},
        {"voice_role":"analyst","line_type":"interpreted","event_ids":["e2"],"text":"It seems MiMo probes."},
    ]
    manifest=build_video_manifest(_replay(),report={"report_url":"r","outcome":{}},model_metadata={},benchmark_snapshot={},commentary=drafted)
    lines=manifest["commentary"]
    assert [line["text"] for line in lines]==["DeepSeek defends.","It seems MiMo probes."]
    assert {line["line_type"] for line in lines}=={"observed","interpreted"}
    assert all(line["event_ids"] for line in lines)
    assert all(left["end_frame"]<=right["start_frame"] for left,right in zip(lines,lines[1:]))


def test_video_manifest_falls_back_when_draft_is_ungrounded():
    manifest=build_video_manifest(_replay(),report={"report_url":"r","outcome":{}},model_metadata={},benchmark_snapshot={},commentary=[{"voice_role":"play_by_play","line_type":"observed","event_ids":["ghost"],"text":"hi"}])
    lines=manifest["commentary"]
    assert all(line["text"]!="hi" for line in lines)
    assert lines and all(line["provenance"]=="deterministic_fallback" for line in lines)


def test_video_manifest_packs_drafted_commentary_into_a_flowing_dialogue():
    drafted=[
        {"voice_role":"play_by_play","line_type":"observed","event_ids":["e1"],"text":"Defends."},
        {"voice_role":"analyst","line_type":"interpreted","event_ids":["e2"],"text":"It seems probes."},
    ]
    manifest=build_video_manifest(_replay(),report={"report_url":"r","outcome":{}},model_metadata={},benchmark_snapshot={},commentary=drafted)
    lines=manifest["commentary"]
    # Packed back-to-back: a short natural pause, not the ~2s event spacing.
    gap=lines[1]["start_frame"]-lines[0]["end_frame"]
    assert 0 < gap <= 15
    # Durations preserved (not squeezed to a 1s minimum).
    assert all(line["end_frame"]-line["start_frame"]>=45 for line in lines)


def test_video_manifest_drops_drafted_commentary_that_overflows_the_match():
    manifest=build_video_manifest(_replay(),report={"report_url":"r","outcome":{}},model_metadata={},benchmark_snapshot={},commentary=[{"voice_role":"play_by_play","line_type":"observed","event_ids":["e1"],"text":" ".join(["word"]*100)}])
    lines=manifest["commentary"]
    assert all(len(line["text"].split())<=25 for line in lines)
    assert lines and all(line["provenance"]=="deterministic_fallback" for line in lines)


def test_video_manifest_schedules_intro_commentary_before_the_match():
    intro = [
        {"voice_role":"play_by_play","line_type":"editorial","scene":"model_cards_and_rules","offset_seconds":1.0,"text":"Welcome back."},
        {"voice_role":"analyst","line_type":"editorial","scene":"model_cards_and_rules","offset_seconds":8.0,"text":"Two models, one flag."},
    ]
    manifest=build_video_manifest(_replay(),report={"report_url":"r","outcome":{}},model_metadata={},benchmark_snapshot={},intro_commentary=intro)
    lines=manifest["commentary"]
    match_start=manifest["scenes"][0]["duration_frames"]+manifest["scenes"][1]["duration_frames"]
    intro_lines=[line for line in lines if not line["event_ids"]]
    assert [line["text"] for line in intro_lines]==["Welcome back.","Two models, one flag."]
    assert all(line["start_frame"]<match_start for line in intro_lines)
    assert lines[0]["text"]=="Welcome back."
    assert all(left["end_frame"]<=right["start_frame"] for left,right in zip(lines,lines[1:]))


def test_video_manifest_falls_back_when_intro_scene_is_unknown():
    manifest=build_video_manifest(_replay(),report={"report_url":"r","outcome":{}},model_metadata={},benchmark_snapshot={},intro_commentary=[{"voice_role":"play_by_play","line_type":"editorial","scene":"recap","offset_seconds":1.0,"text":"hi"}])
    intro_lines=[line for line in manifest["commentary"] if not line["event_ids"]]
    assert intro_lines and all(line["provenance"]=="deterministic_fallback" for line in intro_lines)


def test_video_manifest_downgrades_unhedged_interpreted_intro_inside_its_window():
    manifest=build_video_manifest(_replay(),report={"report_url":"r","outcome":{}},model_metadata={},benchmark_snapshot={},intro_commentary=[{"voice_role":"analyst","line_type":"interpreted","scene":"model_cards_and_rules","offset_seconds":1.0,"text":"Muse will win."}])
    intro_lines=[line for line in manifest["commentary"] if not line["event_ids"]]
    assert [line["text"] for line in intro_lines]==["Muse will win."]
    assert intro_lines[0]["line_type"]=="editorial"
    line_start=intro_lines[0]["start_frame"]
    match_start=manifest["scenes"][0]["duration_frames"]+manifest["scenes"][1]["duration_frames"]
    assert line_start<match_start
    assert intro_lines[0]["provenance"]=="model_draft"


def test_manifest_opens_with_custom_intro_and_model_cards_follow_at_its_offset():
    fps=24
    manifest=build_video_manifest(_replay(),report={"report_url":"r","outcome":{}},model_metadata={},benchmark_snapshot={},fps=fps)
    scenes=manifest["scenes"]
    assert scenes[0]["type"]=="custom_intro"
    assert scenes[0]["duration_frames"]==round(8*fps)==192
    cursor=0; starts=[]
    for scene in scenes:
        starts.append(cursor); cursor+=int(scene["duration_frames"])
    assert starts[1]==scenes[0]["duration_frames"]
    intro_end=scenes[0]["duration_frames"]
    assert all(line["start_frame"]>=intro_end for line in manifest["commentary"])


def test_no_line_is_scheduled_into_the_custom_intro_window():
    intro=[{"voice_role":"play_by_play","line_type":"editorial","scene":"custom_intro","offset_seconds":2.0,"text":"Greeting over the intro video."}]
    manifest=build_video_manifest(_replay(),report={"report_url":"r","outcome":{}},model_metadata={},benchmark_snapshot={},intro_commentary=intro)
    intro_end=manifest["scenes"][0]["duration_frames"]
    assert all(line["text"]!="Greeting over the intro video." for line in manifest["commentary"])
    assert all(line["start_frame"]>=intro_end for line in manifest["commentary"])


def _phased_replay():
    """Blue action, two recorded interviews, then the red attack phase."""
    return {"schema_version":"sandboxer.replay.v1","source_bundle_hash":"a"*64,"panes":[{"identity":"DeepSeek V4 Pro"},{"identity":"MiMo V2.5 Pro"}],"layout":{"split":{"left":.5,"right":.5,"permanent":True}},"frames":[
        {"sequence":1,"at_monotonic_ns":0,"event_id":"e1","event_type":"MATCH_STARTED","phase":"blue","pane":0,"text":"defend"},
        {"sequence":2,"at_monotonic_ns":1_000_000_000,"event_id":"e2","event_type":"MODEL_RESPONSE","phase":"blue","pane":1,"text":"inspect"},
        {"sequence":3,"at_monotonic_ns":5_000_000_000,"event_id":"e3","event_type":"INTERVIEW_RECORDED","phase":"interview","pane":0,"text":"we held the line"},
        {"sequence":4,"at_monotonic_ns":7_000_000_000,"event_id":"e4","event_type":"INTERVIEW_RECORDED","phase":"interview","pane":1,"text":"we probed hard"},
        {"sequence":5,"at_monotonic_ns":9_000_000_000,"event_id":"e5","event_type":"PHASE_GATE_OPENED","phase":"red","pane":0,"text":"attack round opens"},
        {"sequence":6,"at_monotonic_ns":11_000_000_000,"event_id":"e6","event_type":"TOOL_CALL","phase":"red","pane":1,"text":"probe: HTTP request on target"},
        {"sequence":7,"at_monotonic_ns":13_000_000_000,"event_id":"e7","event_type":"SUBMISSION_VERIFIED","phase":"red","pane":0,"text":"verify: objective token submission"},
        {"sequence":8,"at_monotonic_ns":15_000_000_000,"event_id":"e8","event_type":"MATCH_FINISHED","phase":"finalizing","pane":0,"text":""},
    ]}


def _windows(scenes):
    """(scene, absolute start frame, absolute end frame) for a scene list."""
    out=[]; cursor=0
    for scene in scenes:
        out.append((scene,cursor,cursor+int(scene["duration_frames"])))
        cursor+=int(scene["duration_frames"])
    return out


def test_match_budgets_scale_with_duration_instead_of_a_fixed_cap():
    fps=30
    def _budget(duration_frames):
        scenes=[{"type":"match","scene_key":"match:1","match_number":1,"duration_frames":duration_frames}]
        return _scene_budgets(scenes,fps)["match:1"]
    floor=_budget(24*fps)   # round(24/12)=2 -> clamped up to the floor
    assert (floor.max_lines,floor.max_words_per_line,floor.max_total_words)==(6,25,120)
    scaled=_budget(84*fps)  # round(84/12)=7 -> proportional
    assert (scaled.max_lines,scaled.max_total_words)==(7,140)
    ceiling=_budget(600*fps)  # round(600/12)=50 -> clamped down to the ceiling
    assert (ceiling.max_lines,ceiling.max_total_words)==(40,800)


def test_long_match_narrates_far_beyond_the_old_fixed_cap():
    frames=[]
    for tick,second in enumerate(range(0,720,10)):
        frames.append({"sequence":tick+1,"at_monotonic_ns":second*1_000_000_000,
                       "event_id":f"e{tick+1}","event_type":"MODEL_RESPONSE",
                       "phase":"blue","pane":tick%2,"text":f"terminal beat {second}"})
    replay={"schema_version":"sandboxer.replay.v1","source_bundle_hash":"a"*64,"panes":[{"identity":"DeepSeek V4 Pro"},{"identity":"MiMo V2.5 Pro"}],"layout":{"split":{"left":.5,"right":.5,"permanent":True}},"frames":frames}
    manifest=build_video_manifest(replay,report={"report_url":"r","outcome":{}},model_metadata={},benchmark_snapshot={})
    match=next(scene for scene in manifest["scenes"] if scene["type"]=="match")
    # 710s of action -> round(710/12)=59, clamped to the 40-line ceiling.
    assert match["duration_frames"]==710*manifest["fps"]
    # The old fixed cap dropped everything past ~10 lines; the scaled budget
    # keeps the full verbatim rundown (36 candidate blocks) inside the window.
    assert len([line for line in manifest["commentary"] if line["event_ids"]])>=30


def test_phased_replay_inserts_interview_and_red_phase_scenes_in_order():
    manifest=build_video_manifest(_phased_replay(),report={"report_url":"r","outcome":{}},model_metadata={},benchmark_snapshot={})
    fps=manifest["fps"]; scenes=manifest["scenes"]
    assert [scene["type"] for scene in scenes]==["custom_intro","model_cards_and_rules","match","interview_card","interviews","red_phase_card","match","factual_recap"]
    assert scenes[2]["scene_key"]=="match:1" and scenes[2]["editing"]=="uncut" and scenes[2]["event_ids"]==["e1","e2"]
    assert scenes[3]["event_ids"]==["e3"] and scenes[3]["duration_frames"]==4*fps
    # The real interview-to-red gap is 4s; the interviews block clamps to its 8s minimum.
    assert scenes[4]["scene_key"]=="interviews:1" and scenes[4]["event_ids"]==["e3","e4"] and scenes[4]["duration_frames"]==8*fps
    assert scenes[5]["event_ids"]==["e5"] and scenes[5]["duration_frames"]==3*fps
    assert scenes[6]["scene_key"]=="match:1:2" and scenes[6]["match_number"]==1 and scenes[6]["editing"]=="uncut" and scenes[6]["event_ids"]==["e5","e6","e7","e8"]
    # Terminal events land inside their owning scene's window.
    def _owner(at_frame):
        return next(scene for scene,start,end in _windows(scenes) if start<=at_frame<end)
    by_event={entry["event_id"]:entry for entry in manifest["terminal"]}
    assert _owner(by_event["e1"]["at_frame"]) is scenes[2] and _owner(by_event["e2"]["at_frame"]) is scenes[2]
    assert _owner(by_event["e3"]["at_frame"]) is scenes[4] and _owner(by_event["e4"]["at_frame"]) is scenes[4]
    assert all(_owner(by_event[event]["at_frame"]) is scenes[6] for event in ("e5","e6","e7"))
    # Budgets cover every narrated scene exactly once, with windows matching durations.
    budgets=_scene_budgets(scenes,fps)
    assert set(budgets)=={"model_cards_and_rules","match:1","interviews:1","match:1:2"}
    interviews=budgets["interviews:1"]
    assert (interviews.max_lines,interviews.max_words_per_line,interviews.max_total_words)==(3,25,70)
    for scene in (scenes[2],scenes[4],scenes[6]):
        budget=budgets[scene["scene_key"]]
        assert budget.window_start_s==0.0 and budget.window_end_s==pytest.approx(scene["duration_frames"]/fps)


def test_interviews_payload_is_populated_from_recorded_frames():
    manifest=build_video_manifest(_phased_replay(),report={"report_url":"r","outcome":{}},model_metadata={},benchmark_snapshot={})
    assert manifest["interviews"]==[
        {"competitor":"DeepSeek V4 Pro","text":"we held the line","event_ids":["e3"]},
        {"competitor":"MiMo V2.5 Pro","text":"we probed hard","event_ids":["e4"]},
    ]


def test_drafted_analyst_reactions_pack_inside_the_interviews_window():
    draft=[
        {"voice_role":"analyst","line_type":"interpreted","event_ids":["e3"],"text":"It seems DeepSeek defended well."},
        {"voice_role":"analyst","line_type":"interpreted","event_ids":["e4"],"text":"It looks like MiMo ran out of ideas."},
    ]
    manifest=build_video_manifest(_phased_replay(),report={"report_url":"r","outcome":{}},model_metadata={},benchmark_snapshot={},commentary=draft)
    scenes=manifest["scenes"]
    interviews=next(scene for scene in scenes if scene["type"]=="interviews")
    _,start,end=next((scene,start,end) for scene,start,end in _windows(scenes) if scene is interviews)
    lines=[line for line in manifest["commentary"] if line["event_ids"]]
    assert [line["event_ids"] for line in lines]==[["e3"],["e4"]]
    assert all(start<=line["start_frame"] and line["end_frame"]<=end for line in lines)
