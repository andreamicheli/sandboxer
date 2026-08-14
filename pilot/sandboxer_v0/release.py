"""Final v0 publication gate, site/paper projection, and reversible pointer."""

from __future__ import annotations

import fcntl
import hashlib
import html
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any,Iterator,Mapping,Sequence

from .evidence import EvidenceFreezeError,verify_evidence_bundle
from .report import build_result_report

class PublicationError(ValueError):pass
def _digest(value:object)->str:return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(",",":"),default=str).encode()).hexdigest()

def build_release(bundle:Any,*,review:Mapping[str,Any],video:Mapping[str,Any],licenses:Sequence[str],known_limitations_signature:str,broadcast:Mapping[str,Any]|None=None)->dict[str,Any]:
    try:evidence=verify_evidence_bundle(bundle.evidence_bundle)
    except (AttributeError,EvidenceFreezeError) as error:raise PublicationError("VALID_FROZEN_BUNDLE_REQUIRED") from error
    source=evidence["bundle_hash"]
    if not review.get("human_approval"):raise PublicationError("HUMAN_APPROVAL_REQUIRED")
    if review.get("claim_flags") or any(not item.get("approved_by") for item in review.get("sources",{}).values()):raise PublicationError("REVIEW_GATE_FAILED")
    if video.get("source_bundle_hash")!=source or not video.get("manifest_hash"):raise PublicationError("VIDEO_BUNDLE_MISMATCH")
    if not licenses:raise PublicationError("LICENSE_GATE_FAILED")
    if len(known_limitations_signature)!=64:raise PublicationError("KNOWN_LIMITATIONS_UNSIGNED")
    teardowns=[event for event in bundle.telemetry if event.get("event_type")=="RUNNER_TEARDOWN"]
    if not teardowns or any(event.get("status")!="destroyed" for event in teardowns):raise PublicationError("RUNNER_TEARDOWN_UNVERIFIED")
    broadcast=broadcast or {}
    if broadcast:
        if not broadcast.get("youtube_video_id") or not broadcast.get("youtube_url"):raise PublicationError("BROADCAST_HANDOFF_INCOMPLETE")
        if broadcast.get("source_bundle_hash") not in (None,source):raise PublicationError("BROADCAST_BUNDLE_MISMATCH")
        if broadcast.get("privacy_status") not in (None,"private","unlisted","public"):raise PublicationError("BROADCAST_PRIVACY_INVALID")
    report=build_result_report(evidence); model=report.model.to_dict(); report_ref={"source_bundle_hash":source,"json":report.json,"html":report.html,"pdf_sha256":hashlib.sha256(report.pdf).hexdigest()}
    replay={"source_bundle_hash":source,"schema":bundle.replay.get("schema_version") or bundle.replay.get("schema"),"hash":_digest(bundle.replay)}
    paper=_paper(model,source); site=_site(model,source,replay,video,video_url=broadcast.get("youtube_url"))
    video_ref={"source_bundle_hash":source,"manifest_hash":video["manifest_hash"]}
    if broadcast:
        video_ref.update({"youtube_video_id":broadcast["youtube_video_id"],"youtube_url":broadcast["youtube_url"],"privacy_status":broadcast.get("privacy_status","unlisted")})
        if broadcast.get("tts_blocks_hash"):video_ref["tts_blocks_hash"]=broadcast["tts_blocks_hash"]
    release={"schema":"sandboxer.release.v0","release_id":"sandboxer-v0.0.1","source_bundle_hash":source,"protocol_version":model["protocol"]["series_schema"],"report":report_ref,"paper":paper,"site":site,"replay":replay,"video":video_ref,"gates":{"validity":True,"hashes":True,"links":True,"redaction":True,"factual_traceability":True,"claim_language":True,"accessibility":True,"licenses":list(licenses),"human_approval":review["human_approval"],"runner_teardown":True},"operator_materials":{"dependency_manifest":"video/package-lock.json + pilot/uv.lock","image_manifest":"digest-pinned local Runner profile","retention_policy":"restricted evidence follows declared retention; public normalized evidence is retained","incident_procedure":"quarantine, preserve evidence, halt publication, issue corrected immutable version","known_limitations_signature":known_limitations_signature}}
    release["release_hash"]=_digest(release);return release

def _paper(model:Mapping[str,Any],source:str)->dict[str,Any]:
    evidence="\n".join(f"| {claim['id']} | {claim['type']} | {', '.join(claim['evidence']['event_ids'])} |" for claim in model["claims"])
    sections=("Claim","Context","Threat model","Protocol","Equivalence","Telemetry","Auditor","Scoring","Limitations","Reproducibility","Ethics","Future validation")
    text="# Sandboxer v0 methodology\n\n"+"\n\n".join(f"## {section}\n\nEvidence-bound discussion for this experimental simulated-CTF configuration." for section in sections)+f"\n\n## Evidence table\n\n| Claim | Type | Evidence |\n|---|---|---|\n{evidence}\n"
    return {"source_bundle_hash":source,"language":"en","markdown":text,"hash":hashlib.sha256(text.encode()).hexdigest()}

def _site(model:Mapping[str,Any],source:str,replay:Mapping[str,Any],video:Mapping[str,Any],*,video_url:str|None=None)->dict[str,Any]:
    chapters="".join(f'<li><a href="#match-{item["match_number"]}">Match {item["match_number"]}</a></li>' for item in model["technical_chapters"])
    corrections=html.escape(model["corrections"]["visible_notice"])
    youtube=f' · <a href="{html.escape(video_url)}">Watch on YouTube</a>' if video_url else ""
    document=f'''<!doctype html><html lang="en"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Sandboxer result</title><main><h1>{html.escape(model["title"])}</h1><p>{html.escape(model["disclaimer"])}</p><nav aria-label="Series and Match navigation"><ul>{chapters}</ul></nav><p><a href="report.html">Canonical report</a> · <a href="replay.json">Replay</a> · <a href="video.mp4">Video</a> · <a href="evidence.json">Evidence</a>{youtube}</p><section aria-labelledby="corrections"><h2 id="corrections">Correction history</h2><p>{corrections}</p></section></main></html>'''
    return {"source_bundle_hash":source,"html":document,"hash":hashlib.sha256(document.encode()).hexdigest(),"accessibility":{"language":"en","landmarks":True,"match_navigation":True}}

class PublicationStore:
    def __init__(self,path:Path)->None:self.path=path
    @contextmanager
    def _locked(self)->Iterator[dict[str,Any]]:
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.path.with_suffix(".lock").open("a+") as lock:
            fcntl.flock(lock,fcntl.LOCK_EX);state=json.loads(self.path.read_text()) if self.path.exists() else {"schema":"sandboxer.publication-pointer.v1","current":None,"history":[],"versions":{}};yield state
            temporary=self.path.with_suffix(".tmp");temporary.write_text(json.dumps(state,sort_keys=True,separators=(",",":")));temporary.chmod(0o600);temporary.replace(self.path);fcntl.flock(lock,fcntl.LOCK_UN)
    def publish(self,release:Mapping[str,Any])->None:
        with self._locked() as state:
            identifier=release["release_id"];state["versions"][identifier]=dict(release);state["history"].append({"from":state["current"],"to":identifier,"action":"publish"});state["current"]=identifier
    def rollback(self,release_id:str)->None:
        with self._locked() as state:
            if release_id not in state["versions"]:raise PublicationError("ROLLBACK_VERSION_UNKNOWN")
            state["history"].append({"from":state["current"],"to":release_id,"action":"rollback"});state["current"]=release_id
    def current(self)->str|None:
        with self._locked() as state:return state["current"]
