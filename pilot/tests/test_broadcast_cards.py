from __future__ import annotations

import pytest

from sandboxer_v0.broadcast_cards import ConceptLedger, ModelSnapshot, cards_for_episode


def test_model_snapshot_requires_exact_complete_cards_and_omits_incomparable_benchmarks():
    snapshot=ModelSnapshot.create(snapshot_date="2026-08-13",models={
        "deepseek/v4":{"public_name":"DeepSeek V4","producer":"DeepSeek","runtime":"deepseek/v4@2026-08","context_length":128000,"weights":"open","license":"MIT"},
        "xiaomi/mimo":{"public_name":"MiMo","producer":"Xiaomi","runtime":"xiaomi/mimo@2026-08","context_length":128000,"weights":"open","license":"Apache-2.0"},
    },benchmarks={"cyber":{"harness_version":"v1","evaluation_date":"2026-08-01","source":"https://primary.test","primary":True,"values":{"deepseek/v4":{"configuration":"deepseek/v4@2026-08","value":.6},"xiaomi/mimo":{"configuration":"xiaomi/mimo@2026-08","value":.5}}},"coding":{"harness_version":"v2","evaluation_date":"2026-08-01","source":"https://primary.test","primary":True,"values":{"deepseek/v4":{"configuration":"deepseek/v4@2026-08","value":.7}}}})
    cards=snapshot.cards(("deepseek/v4","xiaomi/mimo"))
    assert cards[0]["producer"]=="DeepSeek" and [row["benchmark"] for row in cards[0]["benchmarks"]]==["cyber"]


def test_concept_ledger_drafts_do_not_consume_and_publication_is_atomic(tmp_path):
    ledger=ConceptLedger(tmp_path/"concepts.json")
    candidate={"concept_id":"path_traversal","version":1,"plain_language":"A request escapes the intended folder.","metaphor":"Illustratively, it is like using a back stairwell to leave the assigned room.","explanation_hash":"a"*64,"sources":["https://primary.test"],"related_concepts":["filesystem_paths"],"start_frame":120,"end_frame":300,"decisive":False}
    assert [item["concept_id"] for item in cards_for_episode([candidate],published=False)]==["path_traversal"]
    assert ledger.snapshot()=={}
    token=ledger.reserve(episode_id="episode-1",candidates=[candidate]); ledger.commit(token,episode_id="episode-1",timecodes={"path_traversal":"00:00:04"})
    assert ledger.snapshot()["path_traversal"]["first_published_episode"]=="episode-1"
    assert ledger.reserve(episode_id="episode-2",candidates=[candidate])==""


def test_cards_are_bounded_nondecisive_single_upper_center_overlay():
    candidates=[{"concept_id":f"c{i}","version":1,"plain_language":"simple","metaphor":"Illustratively, like a locked door.","explanation_hash":f"{i:064x}","sources":["s"],"related_concepts":[],"start_frame":i*100,"end_frame":i*100+60,"decisive":False} for i in range(12)]
    cards=cards_for_episode(candidates,published=False,maximum_cards=10)
    assert len(cards)==10
    assert all(card["overlay"]=={"anchor":"upper-center","resizes_terminals":False,"dims_terminals":False,"protected_safe_zone":True} for card in cards)
    assert all(left["end_frame"]<=right["start_frame"] for left,right in zip(cards,cards[1:]))
    assert len(cards_for_episode(candidates,published=False))==6
