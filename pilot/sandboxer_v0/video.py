"""Deterministic editorial manifest for the reviewable Series video."""

from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
from dataclasses import asdict, dataclass
from typing import Any, Mapping


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


def build_video_manifest(replay:Mapping[str,Any],*,report:Mapping[str,Any],model_metadata:Mapping[str,Any],benchmark_snapshot:Mapping[str,Any],fps:int=30)->dict[str,Any]:
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
    scenes.append({"type":"factual_recap","duration_frames":12*fps,"winner":report.get("outcome",{}).get("winner"),"report_link":report.get("report_url"),"event_ids":[frames[-1]["event_id"]]})
    commentary=[]; cursor=scenes[0]["duration_frames"]+scenes[1]["duration_frames"]
    scene_offset={}; running=cursor
    for scene in scenes[2:]:
        if scene["type"]=="match": scene_offset[scene["match_number"]]=running
        running+=scene["duration_frames"]
    for index,frame in enumerate(frames):
        if not frame.get("text") or index%2: continue
        length=min(4*fps,max(2*fps,len(str(frame["text"]))//12*fps))
        match_number=frame.get("match_number") or 1
        match_start=min(int(item["at_monotonic_ns"]) for item in matches[match_number])
        at=scene_offset[match_number]+round((int(frame["at_monotonic_ns"])-match_start)/1_000_000_000*fps)
        commentary.append({"voice_role":"analyst" if index and index%5==0 else "play_by_play","model":identities[frame.get("pane",0) or 0],"start_frame":at,"end_frame":at+length,"text":str(frame["text"]),"event_ids":[frame["event_id"]],"line_type":"observed"})
    commentary.sort(key=lambda item:item["start_frame"])
    for previous,current in zip(commentary,commentary[1:]): current["start_frame"]=max(current["start_frame"],previous["end_frame"]+round(.35*fps)); current["end_frame"]=max(current["end_frame"],current["start_frame"]+fps)
    manifest={"schema":"sandboxer.video-manifest.v1","fps":fps,"identities":identities,"source_bundle_hash":replay.get("source_bundle_hash"),"layout":{"split":{"left":.5,"right":.5,"permanent":True}},"timeline":timeline,"terminal":terminal,"scenes":scenes,"commentary":commentary,"silence_allowed":True,"tts":{"expected":asdict(TtsPreflight("gemini-3.1-flash-tts-preview",("Kore","Charon"),"settings-v1")),"blocks":"bounded-and-hashed"},"qa":{"required":["alignment","clipping","noise","speaker_swaps","silence","pronunciation","factual_traceability","accessibility","licensing","decisive_cue_audibility"]},"composition":{"engine":"remotion","ffmpeg":["probe","loudness-normalize","mux","delivery-encode"]}}
    manifest["manifest_hash"]=_digest(manifest);return manifest
