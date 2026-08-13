from __future__ import annotations

import json

import pytest

from sandboxer_v0 import execute_series
from sandboxer_v0.release import PublicationError, PublicationStore, build_release
from test_result_report import _spec


def _inputs():
    bundle=execute_series(_spec()); return bundle, {"human_approval":{"reviewer":"editor","approved_at":"2026-08-13"},"claim_flags":{},"sources":{}}


def test_release_derives_site_and_methodology_from_same_frozen_bundle(tmp_path):
    bundle,review=_inputs(); release=build_release(bundle,review=review,video={"source_bundle_hash":bundle.evidence_bundle["bundle_hash"],"manifest_hash":"a"*64},licenses=("remotion@4.0.509",),known_limitations_signature="b"*64)
    assert release["source_bundle_hash"]==bundle.evidence_bundle["bundle_hash"]
    assert all(release[name]["source_bundle_hash"]==release["source_bundle_hash"] for name in ("site","paper","report","replay","video"))
    assert all(section in release["paper"]["markdown"] for section in ("Claim","Threat model","Protocol","Equivalence","Telemetry","Auditor","Scoring","Limitations","Reproducibility","Ethics","Future validation","Evidence table"))
    assert "global ranking" not in release["site"]["html"].lower()


def test_publication_pointer_is_atomic_reversible_and_preserves_versions(tmp_path):
    bundle,review=_inputs(); release=build_release(bundle,review=review,video={"source_bundle_hash":bundle.evidence_bundle["bundle_hash"],"manifest_hash":"a"*64},licenses=("license",),known_limitations_signature="b"*64)
    store=PublicationStore(tmp_path/"publication.json"); store.publish(release); assert store.current()==release["release_id"]
    corrected={**release,"release_id":"sandboxer-v0.0.2","release_hash":"c"*64}; store.publish(corrected); store.rollback(release["release_id"])
    state=json.loads((tmp_path/"publication.json").read_text()); assert state["current"]==release["release_id"] and set(state["versions"])=={release["release_id"],"sandboxer-v0.0.2"}


def test_release_gate_rejects_missing_human_approval_or_runner_teardown():
    bundle,review=_inputs(); review["human_approval"]=None
    with pytest.raises(PublicationError,match="HUMAN_APPROVAL_REQUIRED"):
        build_release(bundle,review=review,video={"source_bundle_hash":bundle.evidence_bundle["bundle_hash"],"manifest_hash":"a"*64},licenses=("license",),known_limitations_signature="b"*64)
