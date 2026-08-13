"""Durable, reproducible review chain for a frozen public Result Report."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence


class ReviewPipelineError(ValueError): pass


STAGES=("gemini_evidence_triage","luna_source_discovery","sonnet_behavioral_analysis","terra_skeptical_review","sol_final_synthesis")
MODEL_ROLE={"gemini_evidence_triage":"gemini","luna_source_discovery":"luna","sonnet_behavioral_analysis":"sonnet","terra_skeptical_review":"terra","sol_final_synthesis":"sol"}
BLOCKING_CLAIM_REASONS=frozenset({"UNSUPPORTED","CONTRADICTORY","ANTHROPOMORPHIC","OVER_GENERALIZED"})
REFERENCES=(
    {"name":"EnIGMA","scope":"methodology-and-editorial-only","may_validate_sandboxer_claims":False},
    {"name":"NYU CTF Bench","scope":"methodology-and-editorial-only","may_validate_sandboxer_claims":False},
)


def _digest(value:object)->str: return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest()


class ReviewPipeline:
    def __init__(self,path:Path)->None:
        self.path=path; self._state=json.loads(path.read_text(encoding="utf-8"))
        if self._state.get("schema")!="sandboxer.report-review.v1": raise ReviewPipelineError("REVIEW_STATE_INVALID")
    @classmethod
    def create(cls,path:Path,*,report:Mapping[str,Any])->"ReviewPipeline":
        deterministic={"measurements":report.get("measurements",[]),"evidence_tables":report.get("evidence_tables",[])}
        state={"schema":"sandboxer.report-review.v1","deterministic_evidence":deterministic,"deterministic_evidence_hash":_digest(deterministic),"invocations":[],"sources":{},"claim_flags":{},"methodological_references":list(REFERENCES),"human_approval":None}
        path.parent.mkdir(parents=True,exist_ok=True); path.write_text(json.dumps(state,sort_keys=True,separators=(",",":")),encoding="utf-8"); path.chmod(0o600)
        return cls(path)
    @property
    def next_stage(self)->str|None:
        completed={item["stage"] for item in self._state["invocations"]}
        return next((stage for stage in STAGES if stage not in completed),None)
    def snapshot(self)->dict[str,Any]: return json.loads(json.dumps(self._state))
    def invocation(self,invocation_id:str)->dict[str,Any]: return next(item for item in self._state["invocations"] if item["invocation_id"]==invocation_id)
    def record(self,*,stage:str,model:str,version:str,prompt:str,input_hashes:Sequence[str],output:Mapping[str,Any],disagreement:Sequence[str]=(),escalation_reason:str|None=None,context_id:str|None=None)->str:
        if stage!=self.next_stage: raise ReviewPipelineError("REVIEW_STAGE_ORDER_INVALID")
        if MODEL_ROLE[stage] not in model.lower(): raise ReviewPipelineError("REVIEW_MODEL_ROLE_INVALID")
        if not model or not version or not prompt or self._state["deterministic_evidence_hash"] not in input_hashes: raise ReviewPipelineError("REVIEW_INVOCATION_INCOMPLETE")
        number=len(self._state["invocations"])+1; invocation_id=f"review-{number:02d}"
        record={"invocation_id":invocation_id,"context_id":context_id or invocation_id,"stage":stage,"model":model,"version":version,"prompt":prompt,"input_hashes":list(input_hashes),"structured_output":dict(output),"disagreement":list(disagreement),"escalation_reason":escalation_reason}
        record["record_hash"]=_digest(record); self._state["invocations"].append(record); self._write(); return invocation_id
    def discover_source(self,*,source_id:str,url:str,title:str,version:str,access_date:str,archive_hash:str,primary:bool,claim_ids:Sequence[str],invocation_id:str)->None:
        invocation=self.invocation(invocation_id)
        if invocation["stage"]!="luna_source_discovery" or not re.fullmatch(r"[0-9a-f]{64}",archive_hash): raise ReviewPipelineError("SOURCE_DISCOVERY_INVALID")
        self._state["sources"][source_id]={"url":url,"title":title,"version":version,"access_date":access_date,"archive_hash":archive_hash,"primary":primary,"claim_ids":list(claim_ids),"discovered_by":invocation_id,"discovery_context":invocation["context_id"],"approved_by":None}; self._write()
    def approve_source(self,source_id:str,*,invocation_id:str,context_id:str)->None:
        source=self._state["sources"][source_id]
        approval=self.invocation(invocation_id)
        if invocation_id==source["discovered_by"] or context_id==source["discovery_context"]: raise ReviewPipelineError("SOURCE_INDEPENDENT_APPROVAL_REQUIRED")
        if approval["context_id"]!=context_id or approval["stage"] not in {"terra_skeptical_review","sol_final_synthesis"}: raise ReviewPipelineError("SOURCE_APPROVER_INVALID")
        if not source["primary"] or not source["version"] or not source["access_date"] or not source["archive_hash"]: raise ReviewPipelineError("SOURCE_PRIMARY_ARCHIVE_REQUIRED")
        source["approved_by"]={"invocation_id":invocation_id,"context_id":context_id}; self._write()
    def flag_claim(self,claim_id:str,*,reason:str)->None:
        if reason not in BLOCKING_CLAIM_REASONS: raise ReviewPipelineError("CLAIM_FLAG_INVALID")
        self._state["claim_flags"][claim_id]=reason; self._write()
    def resolve_claim(self,claim_id:str,*,invocation_id:str,justification:str)->None:
        invocation=self.invocation(invocation_id)
        if invocation["stage"] not in {"terra_skeptical_review","sol_final_synthesis"} or claim_id not in self._state["claim_flags"] or not justification.strip(): raise ReviewPipelineError("CLAIM_RESOLUTION_INVALID")
        del self._state["claim_flags"][claim_id]; self._write()
    def approve_human(self,*,reviewer:str,approved_at:str)->None:
        sources=self._state["sources"].values()
        if self.next_stage is not None or self._state["claim_flags"] or any(item["approved_by"] is None for item in sources): raise ReviewPipelineError("PUBLICATION_REVIEW_BLOCKED")
        self._state["human_approval"]={"reviewer":reviewer,"approved_at":approved_at}; self._write()
    def _write(self)->None:
        temporary=self.path.with_suffix(".tmp"); temporary.write_text(json.dumps(self._state,sort_keys=True,separators=(",",":")),encoding="utf-8"); temporary.chmod(0o600); temporary.replace(self.path)
