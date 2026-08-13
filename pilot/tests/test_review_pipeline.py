from __future__ import annotations

import pytest

from sandboxer_v0.review_pipeline import ReviewPipeline, ReviewPipelineError


def test_review_chain_is_resumable_and_deterministic_evidence_is_not_regenerated(tmp_path):
    path=tmp_path/"review.json"; pipeline=ReviewPipeline.create(path,report={"measurements":[{"id":"m1","value":2}],"evidence_tables":[{"event_id":"e1"}]})
    evidence_hash=pipeline.snapshot()["deterministic_evidence_hash"]
    pipeline.record(stage="gemini_evidence_triage",model="gemini-3.6-flash",version="2026-08-13",prompt="triage",input_hashes=(evidence_hash,),output={"clusters":[]})
    resumed=ReviewPipeline(path)
    assert resumed.next_stage=="luna_source_discovery" and resumed.snapshot()["deterministic_evidence_hash"]==evidence_hash


def test_source_discovery_and_approval_require_independent_context_and_primary_archive(tmp_path):
    pipeline=ReviewPipeline.create(tmp_path/"review.json",report={"measurements":[],"evidence_tables":[]})
    root=pipeline.snapshot()["deterministic_evidence_hash"]
    pipeline.record(stage="gemini_evidence_triage",model="gemini-3.6-flash",version="v",prompt="p",input_hashes=(root,),output={})
    discovery=pipeline.record(stage="luna_source_discovery",model="gpt-5.6-luna",version="v",prompt="find",input_hashes=(root,),output={})
    pipeline.discover_source(source_id="s1",url="https://example.test/paper",title="Primary paper",version="v1",access_date="2026-08-13",archive_hash="a"*64,primary=True,claim_ids=("c1",),invocation_id=discovery)
    with pytest.raises(ReviewPipelineError,match="SOURCE_INDEPENDENT_APPROVAL_REQUIRED"):
        pipeline.approve_source("s1",invocation_id=discovery,context_id=pipeline.invocation(discovery)["context_id"])


def test_unsafe_claims_block_human_approval_and_references_are_scoped(tmp_path):
    pipeline=ReviewPipeline.create(tmp_path/"review.json",report={"measurements":[],"evidence_tables":[]})
    assert {item["name"] for item in pipeline.snapshot()["methodological_references"]}=={"EnIGMA","NYU CTF Bench"}
    pipeline.flag_claim("c1",reason="ANTHROPOMORPHIC")
    with pytest.raises(ReviewPipelineError,match="PUBLICATION_REVIEW_BLOCKED"):
        pipeline.approve_human(reviewer="editor",approved_at="2026-08-13")

def test_exact_review_roles_source_approval_claim_resolution_and_human_gate(tmp_path):
    pipeline=ReviewPipeline.create(tmp_path/"review.json",report={"measurements":[],"evidence_tables":[]}); root=pipeline.snapshot()["deterministic_evidence_hash"]
    ids=[]
    for stage,model in (("gemini_evidence_triage","gemini-3.6-flash"),("luna_source_discovery","gpt-5.6-luna"),("sonnet_behavioral_analysis","claude-sonnet-4.6"),("terra_skeptical_review","gpt-5.6-terra"),("sol_final_synthesis","gpt-5.6-sol")):
        ids.append(pipeline.record(stage=stage,model=model,version="pinned",prompt="review",input_hashes=(root,),output={}))
    pipeline.discover_source(source_id="primary",url="https://example.test",title="Paper",version="v1",access_date="2026-08-13",archive_hash="a"*64,primary=True,claim_ids=("c1",),invocation_id=ids[1])
    pipeline.approve_source("primary",invocation_id=ids[3],context_id=ids[3])
    pipeline.flag_claim("c1",reason="UNSUPPORTED"); pipeline.resolve_claim("c1",invocation_id=ids[4],justification="claim removed")
    pipeline.approve_human(reviewer="editor",approved_at="2026-08-13")
    assert pipeline.snapshot()["human_approval"]["reviewer"]=="editor"
