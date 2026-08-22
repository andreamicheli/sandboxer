"""Deterministic editorial manifest for the reviewable Series video."""

from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

from .render_preflight import RenderPreflightPlan, preflight_render
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


# Broadcast voice contract defaults: a run with no explicit TTS configuration
# uses Fish Audio's free ``s2.1-pro-free`` tier (Kore = play-by-play, Charon =
# analyst).  Gemini remains available only via an explicit provider override.
DEFAULT_TTS_PROVIDER = "fish"
DEFAULT_TTS_MODEL = "s2.1-pro-free"
DEFAULT_TTS_VOICES = ("Kore", "Charon")
DEFAULT_TTS_SETTINGS_VERSION = "fish-audio-v1"


@dataclass(frozen=True)
class TtsPreflight:
    model:str
    voices:tuple[str,str]
    settings_version:str
    def verify(self,observed:"TtsPreflight")->None:
        if self!=observed: raise VideoError("TTS_PREFLIGHT_DRIFT")


@dataclass(frozen=True)
class TtsProvenance:
    """One end of the TTS provenance pair: provider/model/voices.

    ``requested`` is the configuration before synthesis; ``observed`` is what
    the adapter actually used, recorded at call time.  A reviewer reading the
    manifest must be able to tell which provider produced the audio without
    trusting that the requested settings were honored.
    """
    provider:str
    model:str
    voices:tuple[str,...]


def _provenance_view(value:Any)->dict[str,Any]|None:
    """Normalize a provenance mapping for comparison (JSON round-trip safe).

    JSON turns voice tuples into lists, and voice order is not semantic, so
    voices are compared as sorted tuples of strings.
    """
    if not isinstance(value,Mapping) or not value: return None
    return {"provider":str(value.get("provider","")),
            "model":str(value.get("model","")),
            "voices":tuple(sorted(str(voice) for voice in value.get("voices") or ()))}


def provenance_drift(requested:Any,observed:Any)->bool:
    """Whether the audio could not have come from the requested settings.

    A different provider or model is drift, and so is any voice the request
    never asked for.  A requested-but-unspoken voice is not drift: a short
    episode may simply never reach the analyst role.
    """
    req=_provenance_view(requested)
    obs=_provenance_view(observed)
    if req is None or obs is None: return False
    return (req["provider"]!=obs["provider"]
            or req["model"]!=obs["model"]
            or bool(set(obs["voices"])-set(req["voices"])))


def tts_provenance_section(requested:TtsProvenance,observed:TtsProvenance)->dict[str,Any]:
    """Build the manifest TTS provenance section with its drift flag."""
    return {"requested":asdict(requested),"observed":asdict(observed),
            "tts_provider_drift":provenance_drift(asdict(requested),asdict(observed))}


def validate_provenance(manifest:Mapping[str,Any])->list[str]:
    """Audit a manifest's TTS provenance; returns a list of problems.

    Flags: observed section missing entirely, an empty observed model,
    a drift flag set true while requested matches observed, or left false
    while requested differs from observed.  An empty list means the
    provenance is internally consistent and a reviewer can trust both halves.
    """
    tts=manifest.get("tts") if isinstance(manifest,Mapping) else None
    if not isinstance(tts,Mapping): return ["TTS_PROVENANCE_SECTION_MISSING"]
    problems:list[str]=[]
    observed=_provenance_view(tts.get("observed"))
    if observed is None:
        problems.append("TTS_PROVENANCE_OBSERVED_MISSING")
        return problems
    if not observed["model"].strip():
        problems.append("TTS_PROVENANCE_OBSERVED_MODEL_EMPTY")
        return problems
    drift=tts.get("tts_provider_drift")
    differs=provenance_drift(tts.get("requested"),tts.get("observed"))
    if drift is True and not differs:
        problems.append("TTS_PROVENANCE_DRIFT_FLAG_INCONSISTENT")
    elif not drift and differs:
        problems.append("TTS_PROVENANCE_DRIFT_FLAG_UNSET")
    return problems


def tts_block(*,script:str,model:str,voice:str,style:Mapping[str,Any],audio:bytes,duration_ms:int)->dict[str,Any]:
    if not script.strip() or not audio or not 1<=duration_ms<=30_000: raise VideoError("TTS_BLOCK_INVALID")
    return {"script":script,"model":model,"voice":voice,"style":dict(style),"duration_ms":duration_ms,"script_hash":_digest({"script":script,"model":model,"voice":voice,"style":style}),"audio_sha256":hashlib.sha256(audio).hexdigest()}


def remotion_render_command(*,output:str,props:str,entry:str="src/index.tsx",composition_id:str="SandboxerSeries",plan:RenderPreflightPlan|None=None)->tuple[str,...]:
    """``npx remotion render`` argv with a resource-safe ``--concurrency``.

    The concurrency lane count comes from
    :func:`sandboxer_v0.render_preflight.preflight_render` (RAM tier), so a
    render can never OOM by spawning more Chromium lanes than the host
    sustains.  Pass ``plan`` to reuse an already-computed plan.
    """
    if plan is None: plan=preflight_render()
    return ("npx","remotion","render",entry,composition_id,output,f"--props={props}",f"--concurrency={plan.concurrency}")


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
            # Coverage scales with duration — a fixed cap dropped every line
            # past ~10 on long matches, leaving over half the video without
            # narration — while the floor keeps short matches bounded.
            max_lines=min(40,max(6,round(duration_s/12)))
            budgets[str(scene["scene_key"])]=LineBudget(
                max_lines=max_lines,max_words_per_line=25,max_total_words=max_lines*20,
                window_start_s=0.0,window_end_s=max(1e-6,duration_s))
        elif kind=="interviews":
            # Analyst reactions over the fullscreen interview block: at most a
            # few short lines, anchored to the interview event ids.
            budgets[str(scene["scene_key"])]=LineBudget(
                max_lines=3,max_words_per_line=25,max_total_words=70,
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
    rule=f"Sandboxer is a simulated capture-the-flag. First, {identities[0]} and {identities[1]} defend their own service. Then they attack until both capture the flag or one exhausts its declared budget."
    scenes=[
        {"type":"cold_open","duration_frames":8*fps,"event_ids":[frames[-1]["event_id"]]},
        {"type":"model_cards_and_rules","duration_frames":20*fps,"identities":identities,"metadata":{name:model_metadata.get(name,{}) for name in identities},"benchmarks":_benchmarks(benchmark_snapshot,identities),"rules_sentence":rule},
    ]
    matches:dict[int,list[Mapping[str,Any]]]={}
    for frame in frames: matches.setdefault(frame.get("match_number") or 1,[]).append(frame)
    def _is_interview(frame:Mapping[str,Any])->bool:
        return str(frame.get("phase",""))=="interview" or str(frame.get("event_type",""))=="INTERVIEW_RECORDED"
    # Per-match editorial flow: telemetry phases {blue, interview, red} become
    # blue action up to the first interview event, an interview title card, a
    # fullscreen interview block, then a red-phase title card before attack
    # action resumes.  Each segment is its own top-level scene so terminal
    # filtering by localBase window stays exact; replays without interview or
    # red events keep the single uncut match scene.  ``scene_key`` is the
    # packing key shared by scene_starts, budgets and scheduled blocks — it
    # must be unique per scene instance now that one match can span several
    # match scenes.  ``anchor_windows`` maps each frame timestamp onto its
    # owning scene (ns-low inclusive, ns-high exclusive, packing key).
    anchor_windows:dict[int,list[tuple[int,int,str]]]={}
    for position,(number,match_frames) in enumerate(sorted(matches.items())):
        first_ns=int(match_frames[0]["at_monotonic_ns"]); last_ns=int(match_frames[-1]["at_monotonic_ns"])
        interview_frames=[frame for frame in match_frames if _is_interview(frame)]
        t_iv=int(interview_frames[0]["at_monotonic_ns"]) if interview_frames else None
        red_frames=[frame for frame in match_frames if str(frame.get("phase",""))=="red" and (t_iv is None or int(frame["at_monotonic_ns"])>=t_iv)]
        t_red=int(red_frames[0]["at_monotonic_ns"]) if red_frames else None
        segments:list[dict[str,Any]]=[]; windows:list[tuple[int,int,str]]=[]
        parts=0
        def _match_segment(lo_ns:int,hi_ns:int,frames_slice:list[Mapping[str,Any]])->None:
            nonlocal parts
            parts+=1
            key=f"match:{number}" if parts==1 else f"match:{number}:{parts}"
            segments.append({"type":"match","scene_key":key,"match_number":number,
                             "duration_frames":max(1,round((hi_ns-lo_ns)/1_000_000_000*fps)),
                             "editing":"uncut","event_ids":[frame["event_id"] for frame in frames_slice]})
            windows.append((lo_ns,hi_ns,key))
        cut=t_iv if t_iv is not None else t_red
        if cut is not None:
            pre=[frame for frame in match_frames if int(frame["at_monotonic_ns"])<cut]
            if pre: _match_segment(first_ns,cut,pre)
        else:
            _match_segment(first_ns,last_ns+1,list(match_frames))
        if t_iv is not None:
            iv_end=t_red if t_red is not None else last_ns+1
            iv_duration=min(45*fps,max(8*fps,round((iv_end-t_iv)/1_000_000_000*fps)))
            segments.append({"type":"interview_card","match_number":number,"duration_frames":4*fps,"event_ids":[interview_frames[0]["event_id"]]})
            segments.append({"type":"interviews","scene_key":f"interviews:{number}","match_number":number,
                             "duration_frames":iv_duration,"editing":"fullscreen",
                             "event_ids":[frame["event_id"] for frame in interview_frames]})
            windows.append((t_iv,iv_end,f"interviews:{number}"))
        if t_red is not None:
            segments.append({"type":"red_phase_card","match_number":number,"duration_frames":3*fps,"event_ids":[red_frames[0]["event_id"]]})
            post=[frame for frame in match_frames if int(frame["at_monotonic_ns"])>=t_red]
            if post: _match_segment(t_red,last_ns+1,post)
        scenes.extend(segments)
        anchor_windows[number]=windows
        if position<len(matches)-1: scenes.append({"type":"intermission","duration_frames":60*fps,"target_seconds":60,"event_ids":[match_frames[-1]["event_id"]]})
    scenes.append({"type":"factual_recap","duration_frames":12*fps,"winner":report.get("outcome",{}).get("winner"),"outcome_basis":report.get("outcome",{}).get("basis",""),"report_link":report.get("report_url"),"event_ids":[frames[-1]["event_id"]]})
    cursor=scenes[0]["duration_frames"]+scenes[1]["duration_frames"]
    running=cursor
    scene_starts:dict[str,int]={"cold_open":0,"model_cards_and_rules":int(scenes[1]["duration_frames"])}
    narrated_spans:list[tuple[str,int,int]]=[]  # (packing key, absolute start frame, duration) of every scene that can carry narration
    for scene in scenes[2:]:
        if "scene_key" in scene:
            key=str(scene["scene_key"])
            scene_starts[key]=running
            narrated_spans.append((key,running,int(scene["duration_frames"])))
        running+=scene["duration_frames"]
    # Per-pane terminal feed for the Remotion composition: raw per-runner events
    # (pane, phase, text) placed at their owning scene's absolute window so the
    # renderer's localBase filtering selects exactly the events of each scene.
    event_frame={str(frame["event_id"]):frame for frame in frames}
    def _anchor(frame:Mapping[str,Any])->int:
        """Absolute manifest frame of a replay frame inside its owning scene."""
        ns=int(frame["at_monotonic_ns"])
        windows=anchor_windows[frame.get("match_number") or 1]
        for lo_ns,hi_ns,key in windows:
            if ns<hi_ns: return scene_starts[key]+max(0,round((ns-lo_ns)/1_000_000_000*fps))
        lo_ns,_,key=windows[-1]
        return scene_starts[key]+max(0,round((ns-lo_ns)/1_000_000_000*fps))
    def _locate(at_frame:int)->tuple[str,float]:
        """Map an absolute frame onto its owning narrated scene (key, seconds-in)."""
        for key,start,duration in narrated_spans:
            if at_frame<start+duration: return key,max(0.0,(at_frame-start)/fps)
        key,start,duration=narrated_spans[-1]
        return key,max(0.0,min((at_frame-start)/fps,duration/fps))
    terminal=[{"at_frame":_anchor(frame),"event_id":frame["event_id"],"event_type":frame.get("event_type",""),"phase":frame.get("phase",""),"pane":int(frame.get("pane",0) or 0),"text":frame.get("text","")} for frame in frames]
    interviews=[{"competitor":str(frame.get("competitor") or identities[min(max(int(frame.get("pane",0) or 0),0),len(identities)-1)]),
                 "text":str(frame.get("text","")),"event_ids":[str(frame["event_id"])]}
                for frame in frames if _is_interview(frame)]
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
    manifest={"schema":"sandboxer.video-manifest.v1","fps":fps,"identities":identities,"source_bundle_hash":replay.get("source_bundle_hash"),"layout":{"split":{"left":.5,"right":.5,"permanent":True}},"timeline":timeline,"terminal":terminal,"scenes":scenes,"commentary":scheduled,"interviews":interviews,"arena_visuals":dict(arena_visuals) if arena_visuals else None,"silence_allowed":True,"tts":{"expected":asdict(TtsPreflight(DEFAULT_TTS_MODEL,DEFAULT_TTS_VOICES,DEFAULT_TTS_SETTINGS_VERSION)),"requested":asdict(TtsProvenance(DEFAULT_TTS_PROVIDER,DEFAULT_TTS_MODEL,DEFAULT_TTS_VOICES)),"observed":None,"tts_provider_drift":False,"blocks":"bounded-and-hashed"},"qa":{"required":["alignment","clipping","noise","speaker_swaps","silence","pronunciation","factual_traceability","accessibility","licensing","decisive_cue_audibility"]},"composition":{"engine":"remotion","ffmpeg":["probe","loudness-normalize","mux","delivery-encode"]}}
    manifest["manifest_hash"]=_digest(manifest);return manifest
