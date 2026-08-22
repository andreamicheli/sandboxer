"""Deterministic editorial manifest for the reviewable Series video."""

from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

from .commentary import (
    COMMENTARY_LINE_TYPES,
    COMMENTARY_ROLES,
    INTRO_SCENES,
    fallback_intro_commentary,
    repair_line_types,
)
from .schedule import (
    LineBudget,
    PackedSchedule,
    PROVENANCE_FALLBACK,
    PROVENANCE_MODEL_DRAFT,
    validate_and_pack,
)


class VideoError(ValueError): pass
def _digest(value:object)->str:return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest()


@dataclass(frozen=True)
class TtsPreflight:
    model:str
    voices:tuple[str,str]
    settings_version:str
    def verify(self,observed:"TtsPreflight")->None:
        if self!=observed: raise VideoError("TTS_PREFLIGHT_DRIFT")


def tts_block(*,script:str,model:str,voice:str,style:Mapping[str,Any],audio:bytes,duration_ms:int)->dict[str,Any]:
    if not script.strip() or not audio or not 1<=duration_ms<=30_000: raise VideoError("TTS_BLOCK_INVALID")
    return {"script":script,"model":model,"voice":voice,"style":dict(style),"duration_ms":duration_ms,"script_hash":_digest({"script":script,"model":model,"voice":voice,"style":style}),"audio_sha256":hashlib.sha256(audio).hexdigest()}


def ffmpeg_preflight(executable:str="ffmpeg",*,required_major:int=7)->dict[str,Any]:
    try: completed=subprocess.run((executable,"-version"),capture_output=True,text=True,timeout=5,check=True)
    except (OSError,subprocess.SubprocessError) as error: raise VideoError("FFMPEG_PREFLIGHT_FAILED") from error
    match=re.search(r"ffmpeg version\s+(\d+)",completed.stdout)
    if not match or int(match.group(1))!=required_major: raise VideoError("FFMPEG_VERSION_DRIFT")
    return {"executable":executable,"major":required_major,"version_line":completed.stdout.splitlines()[0],"version_hash":hashlib.sha256(completed.stdout.splitlines()[0].encode()).hexdigest()}


def ffmpeg_delivery_commands(*,video_input:str,audio_input:str,master_output:str,delivery_output:str)->tuple[tuple[str,...],...]:
    paths=(video_input,audio_input,master_output,delivery_output)
    if any(not item or item.startswith("-") for item in paths): raise VideoError("FFMPEG_PATH_INVALID")
    return (
        ("ffprobe","-v","error","-show_streams","-of","json",video_input),
        ("ffmpeg","-nostdin","-i",audio_input,"-af","loudnorm=I=-16:LRA=7:TP=-1.5","-c:a","pcm_s24le",f"{audio_input}.normalized.wav"),
        ("ffmpeg","-nostdin","-i",video_input,"-i",f"{audio_input}.normalized.wav","-map","0:v:0","-map","1:a:0","-c:v","copy","-c:a","aac","-b:a","320k",master_output),
        ("ffmpeg","-nostdin","-i",master_output,"-c:v","libx264","-crf","18","-pix_fmt","yuv420p","-c:a","aac","-movflags","+faststart",delivery_output),
    )


def validate_video_qa(manifest:Mapping[str,Any],*,audio_metrics:Mapping[str,float],licensed_assets:bool,captions_complete:bool,observations:Mapping[str,bool]|None=None)->tuple[str,...]:
    failures=[]
    if audio_metrics.get("true_peak_db",math.inf)>-1: failures.append("CLIPPING")
    if audio_metrics.get("integrated_lufs",0)>-14 or audio_metrics.get("integrated_lufs",-99)<-18: failures.append("LOUDNESS")
    if not licensed_assets: failures.append("LICENSING")
    if not captions_complete: failures.append("ACCESSIBILITY")
    if any(not beat.get("event_ids") for beat in manifest.get("timeline",())): failures.append("FACTUAL_TRACEABILITY")
    observed=observations or {}
    for check in ("alignment","noise","speaker_swaps","silence","pronunciation","decisive_cue_audibility"):
        if observed.get(check) is not True: failures.append(check.upper())
    return tuple(failures)


def _benchmarks(snapshot:Mapping[str,Any],identities:tuple[str,str])->list[dict[str,Any]]:
    rows=[]
    for benchmark,values in snapshot.items():
        if not isinstance(values,Mapping) or any(identity not in values for identity in identities): continue
        rows.append({"benchmark":benchmark,"values":{identity:values[identity] for identity in identities}})
    return rows


# Editorial scheduling policy.  All placement authority lives in
# ``schedule.validate_and_pack``; these are only the per-scene budget shapes
# and the flow gaps used when packing dialogue vs. verbatim narration.
_TAIL_MARGIN_SECONDS = 0.75   # keep intro speech clear of the next scene cut
_DIALOGUE_GAP_SECONDS = 0.4   # natural pause between drafted dialogue lines
_NARRATION_GAP_SECONDS = 0.35 # pause between fallback narration blocks


def _scene_budgets(scenes:Sequence[Mapping[str,Any]],fps:int)->dict[str,LineBudget]:
    """Per-scene line budgets; windows are seconds relative to each scene start."""
    budgets:dict[str,LineBudget]={}
    for scene in scenes:
        kind=str(scene["type"]); duration_s=int(scene["duration_frames"])/fps
        if kind=="cold_open":
            budgets["cold_open"]=LineBudget(max_lines=3,max_words_per_line=20,max_total_words=60,
                                            window_start_s=0.0,window_end_s=max(1e-6,duration_s-_TAIL_MARGIN_SECONDS))
        elif kind=="model_cards_and_rules":
            budgets["model_cards_and_rules"]=LineBudget(max_lines=5,max_words_per_line=20,max_total_words=100,
                                                        window_start_s=0.0,window_end_s=max(1e-6,duration_s-_TAIL_MARGIN_SECONDS))
        elif kind=="match":
            # Match windows run the full scene: drafted lines anchor deep inside
            # the action, and the next scene boundary itself is the hard stop.
            budgets[f"match:{int(scene['match_number'])}"]=LineBudget(
                max_lines=10,max_words_per_line=25,max_total_words=220,
                window_start_s=0.0,window_end_s=max(1e-6,duration_s))
    return budgets


def _blocks_to_lines(packed:PackedSchedule,scene_starts:Mapping[str,int],fps:int)->list[dict[str,Any]]:
    """Convert packed second-based blocks into manifest frame-based lines."""
    lines:list[dict[str,Any]]=[]
    for block in packed.blocks:
        base=scene_starts.get(str(block.get("scene")))
        if base is None: continue
        start=base+round(float(block["offset_seconds"])*fps)
        duration=max(1,round((float(block["end_seconds"])-float(block["offset_seconds"]))*fps))
        lines.append({"voice_role":str(block.get("voice_role") or "play_by_play"),
                      "model":str(block.get("model") or ""),
                      "start_frame":start,"end_frame":start+duration,
                      "text":str(block.get("text","")),
                      "event_ids":[str(item) for item in block.get("event_ids",())],
                      "line_type":str(block.get("line_type") or "observed"),
                      "provenance":str(block.get("provenance") or PROVENANCE_MODEL_DRAFT)})
    lines.sort(key=lambda item:(item["start_frame"],item["end_frame"]))
    return lines


def _schedule_commentary(commentary:Sequence[Mapping[str,Any]]|None,budgets:Mapping[str,LineBudget],scene_starts:Mapping[str,int],fps:int,candidates:"list[dict[str,Any]]",narration:"list[dict[str,Any]]")->list[dict[str,Any]]:
    """Pack match commentary through the schedule authority — never raises on a bad draft.

    ``candidates`` are content-conforming scene-anchored drafts (possibly empty);
    ``narration`` is the deterministic verbatim-terminal rundown used whole when
    no draft was requested and as fallback when every draft line is rejected.
    """
    if commentary is None:
        packed=validate_and_pack(narration,budgets,gap_s=_NARRATION_GAP_SECONDS)
        for block in packed.blocks: block["provenance"]=PROVENANCE_FALLBACK
    elif candidates:
        packed=validate_and_pack(candidates,budgets,gap_s=_DIALOGUE_GAP_SECONDS,fallback=narration)
    else:
        packed=validate_and_pack(narration,budgets,gap_s=_NARRATION_GAP_SECONDS)
        for block in packed.blocks: block["provenance"]=PROVENANCE_FALLBACK
    return _blocks_to_lines(packed,scene_starts,fps)


def _schedule_intro_commentary(intro:Sequence[Mapping[str,Any]],budgets:Mapping[str,LineBudget],scene_starts:Mapping[str,int],fps:int,identities:Sequence[str])->list[dict[str,Any]]:
    """Pack intro commentary through the schedule authority.

    Intro lines can never be scheduled after their scene window ends, so a
    malformed draft can never bleed offsets into the match scenes: out-of-window
    offsets are clamped, non-conforming lines dropped, and an all-rejected draft
    falls back to :func:`fallback_intro_commentary` with every block marked
    ``deterministic_fallback``.
    """
    if not intro: return []
    intro_budgets={scene:budget for scene,budget in budgets.items() if scene in INTRO_SCENES}
    known={str(item) for item in identities}
    candidates:list[dict[str,Any]]=[]
    for line in repair_line_types([dict(item) for item in intro]):
        text=str(line.get("text","")).strip()
        role=line.get("voice_role"); line_type=str(line.get("line_type","editorial"))
        model=line.get("model"); offset=line.get("offset_seconds")
        if role not in COMMENTARY_ROLES or line_type not in COMMENTARY_LINE_TYPES or not text: continue
        if model not in (None,"") and str(model) not in known: continue
        if isinstance(offset,bool) or not isinstance(offset,(int,float)): continue
        candidates.append({"scene":str(line.get("scene")),"offset_seconds":float(offset),
                           "text":text,"voice_role":role,"line_type":line_type,
                           "model":"" if model is None else str(model),"event_ids":[]})
    if candidates:
        packed=validate_and_pack(candidates,intro_budgets,gap_s=_DIALOGUE_GAP_SECONDS,
                                 fallback=fallback_intro_commentary(identities))
    else:
        packed=validate_and_pack(fallback_intro_commentary(identities),intro_budgets,gap_s=_DIALOGUE_GAP_SECONDS)
        for block in packed.blocks: block["provenance"]=PROVENANCE_FALLBACK
    return [line for line in _blocks_to_lines(packed,scene_starts,fps) if not line["event_ids"]]


def build_video_manifest(replay:Mapping[str,Any],*,report:Mapping[str,Any],model_metadata:Mapping[str,Any],benchmark_snapshot:Mapping[str,Any],fps:int=30,commentary:Sequence[Mapping[str,Any]]|None=None,arena_visuals:Mapping[str,Any]|None=None,intro_commentary:Sequence[Mapping[str,Any]]|None=None)->dict[str,Any]:
    if replay.get("schema_version")!="sandboxer.replay.v1" or fps<24: raise VideoError("VIDEO_INPUT_INVALID")
    panes=replay.get("panes",())
    if not isinstance(panes,(list,tuple)) or len(panes)!=2: raise VideoError("VIDEO_IDENTITIES_INVALID")
    identities=tuple(str(pane["identity"]) for pane in panes)
    if any(item.lower() in {"alpha","beta"} for item in identities): raise VideoError("VIDEO_INTERNAL_ROLE_EXPOSED")
    frames=list(replay.get("frames",()))
    if not frames: raise VideoError("VIDEO_TIMELINE_EMPTY")
    start=int(frames[0]["at_monotonic_ns"])
    timeline=[{"at_frame":round((int(frame["at_monotonic_ns"])-start)/1_000_000_000*fps),"event_ids":[frame["event_id"]],"visual":"terminal_event","caption":frame.get("text","")} for frame in frames]
    # Per-pane terminal feed for the Remotion composition: the timeline carries
    # editorial captions, the terminal carries raw per-runner events (pane,
    # phase, text) so each split terminal shows its own live activity.
    terminal=[{"at_frame":round((int(frame["at_monotonic_ns"])-start)/1_000_000_000*fps),"event_id":frame["event_id"],"event_type":frame.get("event_type",""),"phase":frame.get("phase",""),"pane":int(frame.get("pane",0) or 0),"text":frame.get("text","")} for frame in frames]
    rule=f"Sandboxer is a simulated capture-the-flag. First, {identities[0]} and {identities[1]} defend their own service. Then they attack until both capture the flag or one exhausts its declared budget."
    scenes=[
        {"type":"cold_open","duration_frames":8*fps,"event_ids":[frames[-1]["event_id"]]},
        {"type":"model_cards_and_rules","duration_frames":20*fps,"identities":identities,"metadata":{name:model_metadata.get(name,{}) for name in identities},"benchmarks":_benchmarks(benchmark_snapshot,identities),"rules_sentence":rule},
    ]
    matches:dict[int,list[Mapping[str,Any]]]={}
    for frame in frames: matches.setdefault(frame.get("match_number") or 1,[]).append(frame)
    for position,(number,match_frames) in enumerate(sorted(matches.items())):
        duration=max(1,round((int(match_frames[-1]["at_monotonic_ns"])-int(match_frames[0]["at_monotonic_ns"]))/1_000_000_000*fps))
        scenes.append({"type":"match","match_number":number,"duration_frames":duration,"editing":"uncut","event_ids":[frame["event_id"] for frame in match_frames]})
        if position<len(matches)-1: scenes.append({"type":"intermission","duration_frames":60*fps,"target_seconds":60,"event_ids":[match_frames[-1]["event_id"]]})
    scenes.append({"type":"factual_recap","duration_frames":12*fps,"winner":report.get("outcome",{}).get("winner"),"outcome_basis":report.get("outcome",{}).get("basis",""),"report_link":report.get("report_url"),"event_ids":[frames[-1]["event_id"]]})
    cursor=scenes[0]["duration_frames"]+scenes[1]["duration_frames"]
    scene_offset={}; running=cursor
    scene_starts:dict[str,int]={"cold_open":0,"model_cards_and_rules":int(scenes[1]["duration_frames"])}
    match_spans:list[tuple[str,int,int]]=[]
    for scene in scenes[2:]:
        if scene["type"]=="match":
            key=f"match:{int(scene['match_number'])}"
            scene_offset[scene["match_number"]]=running
            scene_starts[key]=running
            match_spans.append((key,running,int(scene["duration_frames"])))
        running+=scene["duration_frames"]
    event_frame={str(frame["event_id"]):frame for frame in frames}
    def _anchor(frame:Mapping[str,Any])->int:
        match_number=frame.get("match_number") or 1
        match_start=min(int(item["at_monotonic_ns"]) for item in matches[match_number])
        return scene_offset[match_number]+round((int(frame["at_monotonic_ns"])-match_start)/1_000_000_000*fps)
    def _locate(at_frame:int)->tuple[str,float]:
        """Map an absolute frame onto its owning match scene (key, seconds-in)."""
        for key,start,duration in match_spans:
            if at_frame<start+duration: return key,max(0.0,(at_frame-start)/fps)
        key,start,duration=match_spans[-1]
        return key,max(0.0,min((at_frame-start)/fps,duration/fps))
    def _narration()->list[dict[str,Any]]:
        # Deterministic fallback: narrate the terminal events verbatim.  Real
        # episodes pass a drafted two-voice commentary instead (see
        # ``sandboxer_v0/commentary.py``).
        blocks=[]
        for index,frame in enumerate(frames):
            text=str(frame.get("text","")).strip()
            if not text or index%2: continue
            scene,offset=_locate(_anchor(frame))
            pane=min(max(int(frame.get("pane",0) or 0),0),len(identities)-1)
            blocks.append({"scene":scene,"offset_seconds":offset,"text":text,
                           "voice_role":"analyst" if index and index%5==0 else "play_by_play",
                           "line_type":"observed","model":identities[pane],
                           "event_ids":[str(frame["event_id"])]})
        return blocks
    candidates:list[dict[str,Any]]=[]
    if commentary is not None:
        for line in repair_line_types([dict(item) for item in commentary]):
            text=str(line.get("text","")).strip()
            eids=[str(item) for item in line.get("event_ids",())]
            anchors=[event_frame[item] for item in eids if item in event_frame]
            if (not text or len(anchors)!=len(eids)
                    or line.get("voice_role") not in COMMENTARY_ROLES
                    or str(line.get("line_type","observed")) not in COMMENTARY_LINE_TYPES): continue
            pane=min(max(int(anchors[0].get("pane",0) or 0),0),len(identities)-1)
            scene,offset=_locate(min(_anchor(frame) for frame in anchors))
            candidates.append({"scene":scene,"offset_seconds":offset,"text":text,
                               "voice_role":line["voice_role"],
                               "line_type":str(line.get("line_type","observed")),
                               "model":str(line.get("model") or identities[pane]),
                               "event_ids":eids})
    budgets=_scene_budgets(scenes,fps)
    match_lines=_schedule_commentary(commentary,budgets,scene_starts,fps,candidates,_narration())
    intro_lines=_schedule_intro_commentary(intro_commentary or (),budgets,scene_starts,fps,identities)
    scheduled=sorted(match_lines+intro_lines,key=lambda item:(item["start_frame"],item["end_frame"]))
    manifest={"schema":"sandboxer.video-manifest.v1","fps":fps,"identities":identities,"source_bundle_hash":replay.get("source_bundle_hash"),"layout":{"split":{"left":.5,"right":.5,"permanent":True}},"timeline":timeline,"terminal":terminal,"scenes":scenes,"commentary":scheduled,"arena_visuals":dict(arena_visuals) if arena_visuals else None,"silence_allowed":True,"tts":{"expected":asdict(TtsPreflight("gemini-3.1-flash-tts-preview",("Kore","Charon"),"settings-v1")),"blocks":"bounded-and-hashed"},"qa":{"required":["alignment","clipping","noise","speaker_swaps","silence","pronunciation","factual_traceability","accessibility","licensing","decisive_cue_audibility"]},"composition":{"engine":"remotion","ffmpeg":["probe","loudness-normalize","mux","delivery-encode"]}}
    manifest["manifest_hash"]=_digest(manifest);return manifest
