from __future__ import annotations

import pytest

from sandboxer_v0.review_pipeline import (
    MODEL_ROLE,
    STAGE_AGENT,
    STAGES,
    STAGE_MODEL,
    ReviewPipeline,
    ReviewPipelineError,
)


def test_review_chain_is_resumable_and_deterministic_evidence_is_not_regenerated(tmp_path):
    path=tmp_path/"review.json"; pipeline=ReviewPipeline.create(path,report={"measurements":[{"id":"m1","value":2}],"evidence_tables":[{"event_id":"e1"}]})
    evidence_hash=pipeline.snapshot()["deterministic_evidence_hash"]
    pipeline.record(stage="gemini_evidence_triage",model=STAGE_MODEL["gemini_evidence_triage"],version="2026-08-13",prompt="triage",input_hashes=(evidence_hash,),output={"clusters":[]})
    resumed=ReviewPipeline(path)
    assert resumed.next_stage=="luna_source_discovery" and resumed.snapshot()["deterministic_evidence_hash"]==evidence_hash


def test_source_discovery_and_approval_require_independent_context_and_primary_archive(tmp_path):
    pipeline=ReviewPipeline.create(tmp_path/"review.json",report={"measurements":[],"evidence_tables":[]})
    root=pipeline.snapshot()["deterministic_evidence_hash"]
    pipeline.record(stage="gemini_evidence_triage",model=STAGE_MODEL["gemini_evidence_triage"],version="v",prompt="p",input_hashes=(root,),output={})
    discovery=pipeline.record(stage="luna_source_discovery",model=STAGE_MODEL["luna_source_discovery"],version="v",prompt="find",input_hashes=(root,),output={})
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
    for stage in STAGES:
        ids.append(pipeline.record(stage=stage,model=STAGE_MODEL[stage],version="pinned",prompt="review",input_hashes=(root,),output={}))
    pipeline.discover_source(source_id="primary",url="https://example.test",title="Paper",version="v1",access_date="2026-08-13",archive_hash="a"*64,primary=True,claim_ids=("c1",),invocation_id=ids[1])
    pipeline.approve_source("primary",invocation_id=ids[3],context_id=ids[3])
    pipeline.flag_claim("c1",reason="UNSUPPORTED"); pipeline.resolve_claim("c1",invocation_id=ids[4],justification="claim removed")
    pipeline.approve_human(reviewer="editor",approved_at="2026-08-13")
    assert pipeline.snapshot()["human_approval"]["reviewer"]=="editor"


def test_stage_models_are_five_distinct_non_authoring_models_per_stage():
    # Five distinct pinned reviewer models, one per stage.
    assert set(STAGE_MODEL)==set(STAGES)
    assert len(set(STAGE_MODEL.values()))==5
    for stage,token in MODEL_ROLE.items():
        # Each role token is contained in its pinned model name (record()'s check).
        assert token in STAGE_MODEL[stage]
    # The ox-alpha authoring model (x-preview-f) never reviews its own work.
    for model in STAGE_MODEL.values():
        assert "x-preview" not in model
    assert set(STAGE_AGENT.values())=={"opencode"}


def test_recorded_model_must_contain_stage_role_token(tmp_path):
    pipeline=ReviewPipeline.create(tmp_path/"review.json",report={"measurements":[],"evidence_tables":[]})
    root=pipeline.snapshot()["deterministic_evidence_hash"]
    with pytest.raises(ReviewPipelineError,match="REVIEW_MODEL_ROLE_INVALID"):
        pipeline.record(stage="gemini_evidence_triage",model="opencode/big-pickle",version="v",prompt="p",input_hashes=(root,),output={})
    pipeline.record(stage="gemini_evidence_triage",model=STAGE_MODEL["gemini_evidence_triage"],version="v",prompt="p",input_hashes=(root,),output={})
    assert pipeline.invocation("review-01")["model"]==STAGE_MODEL["gemini_evidence_triage"]


def test_review_agent_env_override_keeps_recording_honest(monkeypatch,tmp_path):
    pipeline=ReviewPipeline.create(tmp_path/"review.json",report={"measurements":[],"evidence_tables":[]})
    root=pipeline.snapshot()["deterministic_evidence_hash"]
    # Default: stages run on opencode and record that agent.
    monkeypatch.delenv("SANDBOXER_REVIEW_GEMINI_EVIDENCE_TRIAGE_AGENT",raising=False)
    pipeline.record(stage="gemini_evidence_triage",model=STAGE_MODEL["gemini_evidence_triage"],version="v",prompt="p",input_hashes=(root,),output={})
    assert pipeline.invocation("review-01")["agent"]=="opencode"
    with pytest.raises(ReviewPipelineError,match="REVIEW_AGENT_INVALID"):
        pipeline.record(stage="luna_source_discovery",model=STAGE_MODEL["luna_source_discovery"],agent="codex",version="v",prompt="p",input_hashes=(root,),output={})
    # Env override opts the stage back to codex; recording codex is then valid
    # and the record reflects it.
    monkeypatch.setenv("SANDBOXER_REVIEW_LUNA_SOURCE_DISCOVERY_AGENT","codex")
    overridden=pipeline.record(stage="luna_source_discovery",model=STAGE_MODEL["luna_source_discovery"],agent="codex",version="v",prompt="p",input_hashes=(root,),output={})
    assert pipeline.invocation(overridden)["agent"]=="codex"
