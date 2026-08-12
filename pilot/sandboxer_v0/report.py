"""Canonical public Series Result Reports from frozen evidence.

The reporting boundary deliberately accepts an immutable public Evidence Bundle,
not a live Series, Runner, provider, or telemetry file.  One JSON-compatible
model is the sole source for the accessible HTML and self-contained PDF views.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from html import escape
import json
import re
import textwrap
from types import MappingProxyType
from typing import Any, Callable, Iterator, Mapping, Sequence

from .evidence import EvidenceFreezeError, verify_evidence_bundle


class ResultReportError(ValueError):
    """Raised when a public Result Report cannot be derived safely."""


_CLAIM_TYPES = frozenset({"Observed", "Derived", "Interpreted", "Hypothesis"})
_ALIAS = re.compile(r"^(?:alpha|beta)$", re.IGNORECASE)


def _digest(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _json_value(value: object) -> Any:
    """Return the JSON data representation without preserving mutable input."""
    return json.loads(json.dumps(value, sort_keys=True, default=str))


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    if isinstance(value, frozenset):
        return sorted(_thaw(item) for item in value)
    return value


@dataclass(frozen=True)
class ReportModel(Mapping[str, Any]):
    """Deeply immutable typed root for every public report representation."""

    projection: Mapping[str, Any]

    @classmethod
    def from_projection(cls, projection: Mapping[str, Any]) -> "ReportModel":
        return cls(_freeze(projection))

    def __getitem__(self, key: str) -> Any:
        return self.projection[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.projection)

    def __len__(self) -> int:
        return len(self.projection)

    def to_dict(self) -> dict[str, Any]:
        return _thaw(self.projection)


def _label(value: object, fallback: str = "unspecified") -> str:
    # Preserve public Unicode model identities while excluding terminal/control
    # characters that cannot safely appear in HTML or PDF text nodes.
    text = re.sub(r"[\x00-\x1f\x7f]", "?", str(value or "")).strip()
    return text[:180] if text else fallback


def _frozen_valid(bundle: object) -> dict[str, Any]:
    try:
        verified = verify_evidence_bundle(bundle)
    except EvidenceFreezeError as error:
        raise ResultReportError("VALID_FROZEN_EVIDENCE_REQUIRED") from error
    verdict = verified["public"].get("auditor_verdict")
    if not isinstance(verdict, Mapping) or not verdict.get("valid") or not verdict.get("signed"):
        raise ResultReportError("VALID_FROZEN_EVIDENCE_REQUIRED")
    return _json_value(verified)


def _event_ids(events: Sequence[Mapping[str, Any]], predicate: Callable[[Mapping[str, Any]], bool], fallback: str) -> list[str]:
    result = [_label(event.get("event_id"), fallback) for event in events if predicate(event) and event.get("event_id")]
    return result or [fallback]


def _claim(
    claim_id: str,
    claim_type: str,
    text: str,
    event_ids: Sequence[str],
    *,
    granularity: str,
    confidence: str = "high",
    scope: str = "this frozen Series configuration",
    sample_size: int | None = None,
    material_alternatives: Sequence[str] = (),
) -> dict[str, Any]:
    if claim_type not in _CLAIM_TYPES or not event_ids:
        raise ResultReportError("REPORT_CLAIM_INVALID")
    result: dict[str, Any] = {
        "id": claim_id,
        "type": claim_type,
        "text": text,
        "evidence": {"event_ids": list(event_ids), "granularity": granularity},
        "confidence": confidence,
        "scope": scope,
        "sample_size": sample_size,
    }
    if claim_type == "Interpreted":
        result["material_alternatives"] = list(material_alternatives)
    return result


def _competitors(specification: Mapping[str, Any]) -> list[dict[str, Any]]:
    declared = specification.get("competitors")
    if not isinstance(declared, list) or len(declared) != 2:
        raise ResultReportError("PUBLIC_COMPETITOR_MANIFEST_MISSING")
    manifests: list[dict[str, str]] = []
    for number, raw in enumerate(declared, start=1):
        if not isinstance(raw, Mapping):
            raise ResultReportError("PUBLIC_COMPETITOR_MANIFEST_MISSING")
        adapter = raw.get("adapter") if isinstance(raw.get("adapter"), Mapping) else {}
        name = _label(raw.get("public_name"), f"Model {number}")
        model_id = _label(adapter.get("model_id"), f"Model {number}")
        # Alpha/Beta are internal protocol roles and must never become public identities.
        if _ALIAS.fullmatch(name):
            name = model_id
        if _ALIAS.fullmatch(name) or _ALIAS.fullmatch(model_id):
            raise ResultReportError("PUBLIC_COMPETITOR_IDENTITY_INVALID")
        manifests.append({"public_name": name, "model_id": model_id, "adapter_contract": "declared Model Adapter", "adapter_manifest": _json_value(adapter)})
    if len({item["public_name"] for item in manifests}) != 2:
        raise ResultReportError("PUBLIC_COMPETITOR_IDENTITY_INVALID")
    return manifests


def _match_events(events: Sequence[Mapping[str, Any]], match_number: int) -> list[Mapping[str, Any]]:
    return [event for event in events if event.get("match_number") == match_number]


def _report_url(evidence_url: str) -> str:
    match = re.fullmatch(r"sandboxer://evidence/([A-Za-z0-9._-]+)/v([1-9][0-9]*)", evidence_url)
    if not match:
        raise ResultReportError("FROZEN_EVIDENCE_URL_INVALID")
    return f"sandboxer://reports/{match.group(1)}/v{match.group(2)}"


def _semantic_document(model: Mapping[str, Any]) -> dict[str, Any]:
    """Canonical typed document tree consumed by every public renderer."""
    blocks: list[dict[str, Any]] = [
        {"type": "heading", "level": 1, "text": model["title"]},
        {"type": "paragraph", "text": model["scope"]["repeated_scope_language"]},
        {"type": "heading", "level": 2, "text": "Outcome"},
        {"type": "paragraph", "text": f"Winner: {model['outcome']['winner']}"},
        {"type": "link", "label": "Frozen public evidence", "url": model["source_evidence"]["url"]},
    ]
    for claim in model["claims"]:
        blocks.append({"type": "claim", "id": claim["id"], "claim_type": claim["type"], "text": claim["text"], "evidence": claim["evidence"], "metadata": {key: claim[key] for key in ("confidence", "scope", "sample_size", "material_alternatives") if key in claim}})
    for chapter in model["technical_chapters"]:
        blocks.extend([
            {"type": "heading", "level": 2, "text": chapter["heading"]},
            {"type": "table", "caption": "Authoritative Match timeline", "headers": ["Time", "Event", "Phase", "Evidence ID"], "rows": [[event["wall_time_utc"], event["event_type"], event["phase"], event["event_id"]] for event in chapter["timeline"]]},
            {"type": "list", "items": [
                f"Outcome: {json.dumps(_thaw(chapter['outcome']), ensure_ascii=False, sort_keys=True)}",
                f"Blue Brief: {json.dumps(_thaw(chapter['blue_brief']), ensure_ascii=False, sort_keys=True)}",
                f"Role assignment: {json.dumps(_thaw(chapter['role_assignment']), ensure_ascii=False, sort_keys=True)}",
                f"Budgets and accounting: {json.dumps(_thaw(chapter['budgets']), ensure_ascii=False, sort_keys=True)}",
                f"Score proof: {json.dumps(_thaw(chapter['score_proof']), ensure_ascii=False, sort_keys=True)}",
                f"Evidence status: {json.dumps(_thaw(chapter['evidence_status']), ensure_ascii=False, sort_keys=True)}",
                f"Interview comparisons: {json.dumps(_thaw(chapter['interview_red_comparisons']), ensure_ascii=False, sort_keys=True)}",
            ]},
        ])
    blocks.extend([
        {"type": "incident", "items": model["incident_appendix"]},
        {"type": "heading", "level": 2, "text": "Complete reproducibility record"},
        {"type": "list", "items": [
            f"Competitor manifests: {json.dumps(_thaw(model['competitor_manifests']), ensure_ascii=False, sort_keys=True)}",
            f"Protocol and equivalence: {json.dumps({'protocol': _thaw(model['protocol']), 'equivalence': _thaw(model['equivalence'])}, ensure_ascii=False, sort_keys=True)}",
            f"Validity and limitations: {json.dumps({'validity': _thaw(model['validity']), 'limitations': _thaw(model['limitations'])}, ensure_ascii=False, sort_keys=True)}",
            f"Hashes and signature: {json.dumps(_thaw(model['hashes']), ensure_ascii=False, sort_keys=True)}",
            f"Reproducibility links: {json.dumps(_thaw(model['reproducibility_links']), ensure_ascii=False, sort_keys=True)}",
        ]},
        {"type": "heading", "level": 2, "text": "Corrections and provenance"},
        {"type": "paragraph", "text": model["corrections"]["visible_notice"]},
        {"type": "link", "label": "Canonical report", "url": model["corrections"]["current_report_url"]},
    ])
    return {"type": "document", "language": "en", "children": blocks}


def build_result_report(evidence_bundle: object, *, correction_index: Mapping[str, Mapping[str, Any]] | None = None) -> "ResultReport":
    """Generate a canonical English report and all synchronized renderings."""
    bundle = _frozen_valid(evidence_bundle)
    public = bundle["public"]
    specification = public["specification"]
    events = [event for event in public["normalized_telemetry"] if isinstance(event, Mapping)]
    if not events:
        raise ResultReportError("VALID_FROZEN_EVIDENCE_REQUIRED")
    fallback = _label(events[0].get("event_id"), "evidence-root")
    competitors = _competitors(specification)
    score = public.get("score_proof")
    if not isinstance(score, list):
        raise ResultReportError("SCORE_PROOF_MISSING")
    score_by_number = {item.get("match_number"): item for item in score if isinstance(item, Mapping) and isinstance(item.get("match_number"), int)}
    completed = [event for event in events if event.get("event_type") == "MATCH_FINISHED" and isinstance(event.get("match_number"), int)]
    if not completed:
        raise ResultReportError("MATCH_EVIDENCE_MISSING")
    winner = _label(next((event.get("winner") for event in reversed(events) if event.get("event_type") == "SERIES_COMPLETED"), ""), "No valid winner")
    if winner == "No valid winner" or winner not in {item["public_name"] for item in competitors}:
        raise ResultReportError("VALID_SERIES_OUTCOME_MISSING")
    winner_events = _event_ids(events, lambda event: event.get("event_type") == "SERIES_COMPLETED", fallback)
    winning_match = next((event for event in reversed(completed) if event.get("winner") == winner), completed[-1])
    decisive_rule = _label(winning_match.get("reason_code"), "authoritative scoring rule")
    all_match_ids = _event_ids(events, lambda event: event.get("event_type") == "MATCH_FINISHED", fallback)
    policy = specification.get("match_policy") if isinstance(specification.get("match_policy"), Mapping) else {}
    claims: list[dict[str, Any]] = []
    claims.append(_claim(
        "claim-outcome", "Observed", f"{winner} won this Series under the decisive {decisive_rule} rule.", winner_events,
        granularity="Series terminal outcome", sample_size=len(completed),
    ))
    claims.append(_claim(
        "claim-scope", "Observed", "Sandboxer is an experimental benchmark in a simulated CTF Arena with synthetic-only targets.",
        _event_ids(events, lambda event: event.get("event_type") == "SERIES_CREATED", fallback), granularity="Series specification", sample_size=len(completed),
    ))
    claims.append(_claim(
        "claim-equivalence", "Derived", "Both declared Competitors used the same recorded protocol and competitive budget categories.",
        _event_ids(events, lambda event: event.get("event_type") == "BUDGET_OBSERVED", fallback), granularity="cross-Match protocol", sample_size=len(completed),
    ))
    claims.append(_claim(
        "claim-interpretation-limit", "Interpreted", "The recorded outcome supports comparison only for this configuration; it does not establish an intrinsic model personality or general cyber capability.",
        all_match_ids, granularity="cross-Match interpretation", confidence="limited", scope="the declared competitors, adapters, budgets, and Blue Briefs", sample_size=len(completed),
        material_alternatives=("task-family effects", "provider or adapter behavior", "the limited number of Matches"),
    ))
    claims.append(_claim(
        "claim-future", "Hypothesis", "Additional independently frozen Series may help test whether observed patterns recur under compatible conditions.",
        all_match_ids, granularity="future research hypothesis", confidence="not evaluated", scope="future compatible Series", sample_size=len(completed),
    ))

    chapters: list[dict[str, Any]] = []
    incidents: list[dict[str, Any]] = []
    for match in sorted(completed, key=lambda event: int(event["match_number"])):
        number = int(match["match_number"])
        match_events = _match_events(events, number)
        match_id = _label(match.get("event_id"), fallback)
        score_item = score_by_number.get(number, {})
        valid = match.get("reason_code") in {"SOLE_CAPTURE", "DUAL_CAPTURE_HEALTH", "DUAL_CAPTURE_SUBMISSION_ORDER", "NO_CAPTURE_AVAILABILITY", "EXACT_TIE"}
        if not valid:
            incidents.append({
                "match_number": number,
                "reason_code": _label(match.get("reason_code")),
                "evidence_event_ids": _event_ids(match_events, lambda _: True, match_id),
                "comparative_claims": "omitted",
            })
            continue
        start = next((event for event in match_events if event.get("event_type") == "MATCH_STARTED"), {})
        brief = start.get("blue_brief_manifest") if isinstance(start.get("blue_brief_manifest"), Mapping) else {"family": start.get("blue_brief", "unspecified")}
        budgets = [event for event in match_events if event.get("event_type") == "BUDGET_OBSERVED"]
        measurements = {
            "output_tokens": sum(int(event.get("output_tokens", 0)) for event in budgets if isinstance(event.get("output_tokens"), int)),
            "turns": sum(int(event.get("turns", 0)) for event in budgets if isinstance(event.get("turns"), int)),
            "tool_calls": sum(int(event.get("tool_calls", 0)) for event in budgets if isinstance(event.get("tool_calls"), int)),
        }
        phase_accounting = [
            {key: event.get(key) for key in ("event_id", "competitor", "phase", "output_tokens", "turns", "tool_calls")}
            for event in budgets
        ]
        claim_id = f"claim-match-{number}-outcome"
        claims.append(_claim(
            claim_id, "Observed", f"Match {number} finished with {_label(match.get('winner'), 'no winner')} under {_label(match.get('reason_code'))}.",
            [match_id], granularity=f"Match {number}", sample_size=1,
        ))
        comparisons: list[dict[str, Any]] = []
        chapter_claim_ids = [claim_id]
        for competitor in (item["public_name"] for item in competitors):
            interview = next((event for event in match_events if event.get("event_type") == "INTERVIEW_RECORDED" and event.get("competitor") == competitor), None)
            red = next((event for event in match_events if event.get("event_type") == "MODEL_RESPONSE" and event.get("phase") == "red" and event.get("competitor") == competitor and interview and event.get("orchestrator_monotonic_ns", -1) > interview.get("orchestrator_monotonic_ns", -1)), None)
            if not interview or not red:
                continue
            interview_claim_id = f"claim-match-{number}-{competitor}-interview-red"
            claims.append(_claim(
                interview_claim_id, "Observed", f"For {competitor} in Match {number}, a tool-free Interview record precedes a later Red Phase response; this temporal comparison does not establish intent.",
                [str(interview["event_id"]), str(red["event_id"])], granularity=f"Match {number}, {competitor}, Interview then Red Phase", sample_size=1,
            ))
            comparisons.append({"claim_id": interview_claim_id, "competitor": competitor, "interview_event_ids": [str(interview["event_id"])], "red_event_ids": [str(red["event_id"])], "does_not_establish": "intent", "claim_type": "Observed"})
            chapter_claim_ids.append(interview_claim_id)
        timeline = [
            {
                "event_id": _label(event.get("event_id"), match_id), "wall_time_utc": _label(event.get("wall_time_utc")),
                "monotonic_ns": event.get("orchestrator_monotonic_ns"), "event_type": _label(event.get("event_type")),
                "phase": _label(event.get("phase"), "series"), "evidence_kind": _label(event.get("evidence_kind")),
            }
            for event in match_events
        ]
        def evidence_status(event_types: set[str]) -> dict[str, Any]:
            identifiers = [str(event["event_id"]) for event in match_events if event.get("event_type") in event_types and event.get("event_id")]
            return {"event_ids": identifiers, "status": "observed" if identifiers else "not recorded"}
        chapters.append({
            "match_number": number,
            "heading": f"Match {number}: technical chapter",
            "outcome": {"winner": _label(match.get("winner"), "no winner"), "decisive_rule": _label(match.get("reason_code")), "evidence_event_id": match_id},
            "blue_brief": {"family": _label(brief.get("family")), "version": _label(brief.get("version")), "outcome": _label(brief.get("outcome")), "evidence_event_ids": _event_ids(match_events, lambda event: event.get("event_type") == "MATCH_STARTED", match_id)},
            "role_assignment": {"status": "not recorded", "event_ids": []},
            "public_roles": {"identities": [item["public_name"] for item in competitors], "internal_aliases": "not published"},
            "budgets": {"declared": _json_value(policy), "observed_measurements": measurements, "per_competitor_phase_accounting": phase_accounting, "evidence_event_ids": [str(event["event_id"]) for event in budgets if event.get("event_id")]},
            "timeline": timeline,
            "score_proof": _json_value(score_item),
            "faults": {"events": [event["event_id"] for event in match_events if "FAULT" in str(event.get("event_type")) or "ERROR" in str(event.get("event_type"))], "separated_from_competitor_behavior": True},
            "evidence_status": {
                "phase_accounting": evidence_status({"PHASE_GATE_OPENED", "PHASE_ROUND_OPENED", "PHASE_ROUND_CLOSED"}),
                "health": evidence_status({"RUNNER_HEALTH"}),
                "network": evidence_status({"RUNNER_PROBE_RESULT", "PHASE_GATE_OPENED"}),
                "latency_retry_refusal_rate_limit": evidence_status({"PROVIDER_RETRY", "PROVIDER_RATE_LIMIT", "MODEL_REFUSAL", "LATENCY_OBSERVED"}),
                "artifact_references": evidence_status({"ARTIFACT_FROZEN", "EVIDENCE_BUNDLE_FROZEN"}),
            },
            "interview_red_comparisons": comparisons,
            "claim_ids": chapter_claim_ids,
        })

    if not chapters:
        raise ResultReportError("VALID_MATCH_CHAPTER_MISSING")
    score_matches = [
        {
            "match_number": chapter["match_number"],
            "winner": chapter["outcome"]["winner"] if chapter["outcome"]["winner"] != "no winner" else None,
            "reason_code": chapter["outcome"]["decisive_rule"],
            "verified_submission_event_ids": list(score_by_number.get(chapter["match_number"], {}).get("verified_submission_event_ids", [])),
            "final_health_event_ids": list(score_by_number.get(chapter["match_number"], {}).get("final_health_event_ids", [])),
        }
        for chapter in chapters
    ]
    score_proof = {
        "schema": "sandboxer.score-proof.v1",
        "wins": {competitor["public_name"]: sum(match["winner"] == competitor["public_name"] for match in score_matches) for competitor in competitors},
        "matches": score_matches,
    }
    report_url = _report_url(str(bundle["url"]))
    previous = bundle.get("previous_version_url")
    graph_entry = correction_index.get(bundle["url"], {}) if correction_index is not None else {}
    if correction_index is not None and (graph_entry.get("bundle_hash") not in {None, bundle["bundle_hash"]} or graph_entry.get("status", "current") not in {"current", "superseded"}):
        raise ResultReportError("CORRECTION_GRAPH_INVALID")
    correction_status = graph_entry.get("status", "current")
    successor = graph_entry.get("superseded_by")
    if (correction_status == "superseded") != bool(successor):
        raise ResultReportError("CORRECTION_GRAPH_INVALID")
    corrections = {
        "current_report_url": report_url,
        "source_evidence_url": bundle["url"],
        "supersedes_report_url": _report_url(str(previous)) if previous else None,
        "superseded_by_report_url": _report_url(str(successor)) if successor else None,
        "status": correction_status,
        "previous_evidence_hash": bundle.get("previous_bundle_hash"),
        "visible_notice": ("This report is superseded. Follow the visible correction link to the current report; this version remains addressable." if correction_status == "superseded" else "This report is current for its frozen evidence version. Earlier versions remain addressable through the correction chain."),
    }
    model = {
        "schema_version": "sandboxer.result-report.v1",
        "language": "en",
        "title": "Sandboxer Series Result Report",
        "source_evidence": {"url": bundle["url"], "version": bundle["version"], "bundle_hash": bundle["bundle_hash"]},
        "corrections": corrections,
        "scope": {"label": "experimental benchmark in a simulated CTF Arena", "repeated_scope_language": "This experimental benchmark in a simulated CTF Arena is limited to the recorded configuration."},
        "disclaimer": "experimental benchmark in a simulated CTF Arena",
        "outcome": {"winner": winner, "decisive_rule": decisive_rule, "beginner_narrative_claim_ids": ["claim-outcome", "claim-scope", "claim-interpretation-limit"]},
        "competitor_manifests": competitors,
        "equivalence": {"assessment_claim_id": "claim-equivalence", "protocol": "shared declared SeriesSpec", "opportunity": "shared budget categories"},
        "protocol": {"series_schema": _label(specification.get("schema_version")), "best_of": policy.get("best_of"), "phases": ["Blue", "Interview", "Red"], "role_policy": "public competitor identities only"},
        "score_proof": score_proof,
        "narrative_chapters": [{"heading": "Outcome first", "claim_ids": ["claim-outcome", "claim-scope", "claim-interpretation-limit", "claim-future"]}],
        "technical_chapters": chapters,
        "incident_appendix": incidents,
        "validity": {"auditor_verdict": _json_value(public["auditor_verdict"]), "status": "valid signed frozen evidence"},
        "limitations": {"claim_ids": ["claim-interpretation-limit", "claim-future"]},
        "hashes": {"bundle_hash": bundle["bundle_hash"], "checksums": _json_value(bundle["checksums"]), "signature": bundle["signature"]},
        "reproducibility_links": [{"label": "Frozen public evidence", "url": bundle["url"]}, {"label": "Canonical report", "url": report_url}],
        "claims": claims,
        "accessibility": {"language": "en", "landmarks": ["header", "main", "footer"], "claim_citations": "visible", "table_headers": "scoped", "print_layout": "A4-friendly"},
    }
    model["document"] = _semantic_document(model)
    immutable_model = ReportModel.from_projection(model)
    json_text = json.dumps(immutable_model.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    html = render_report_html(immutable_model)
    pdf = render_report_pdf(immutable_model)
    return ResultReport(model=immutable_model, json=json_text, html=html, pdf=pdf)


def _claim_by_id(model: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {claim["id"]: claim for claim in model["claims"] if isinstance(claim, Mapping)}


def render_report_html(model: Mapping[str, Any]) -> str:
    """Render the canonical model as semantic, keyboard-readable print HTML."""
    claims = _claim_by_id(model)
    def rendered_claim(claim_id: str) -> str:
        claim = claims[claim_id]
        evidence = ", ".join(escape(str(item)) for item in claim["evidence"]["event_ids"])
        extras = ""
        if claim["type"] == "Interpreted":
            extras = f"<p>Alternatives: {escape('; '.join(claim['material_alternatives']))}. Confidence: {escape(claim['confidence'])}. Scope: {escape(claim['scope'])}. Sample size: {claim['sample_size']}.</p>"
        return f'<article id="{escape(claim_id)}" class="claim claim-{escape(claim["type"].lower())}" data-claim-id="{escape(claim_id)}"><h3>{escape(claim["type"])} claim</h3><p>{escape(claim["text"])}</p><p class="citation">Evidence ({escape(claim["evidence"]["granularity"])}): {evidence}</p>{extras}</article>'
    outcome_claims = "".join(rendered_claim(identifier) for identifier in model["outcome"]["beginner_narrative_claim_ids"])
    visible_claim_ids = set(model["outcome"]["beginner_narrative_claim_ids"])
    chapters: list[str] = []
    for chapter in model["technical_chapters"]:
        rows = "".join(f"<tr><td>{escape(str(event['wall_time_utc']))}</td><td>{escape(str(event['event_type']))}</td><td>{escape(str(event['phase']))}</td><td>{escape(str(event['event_id']))}</td></tr>" for event in chapter["timeline"])
        chapter_claims = "".join(rendered_claim(identifier) for identifier in chapter["claim_ids"])
        visible_claim_ids.update(chapter["claim_ids"])
        declared = escape(json.dumps(_thaw(chapter["budgets"]["declared"]), sort_keys=True))
        observed = escape(json.dumps(_thaw(chapter["budgets"]["observed_measurements"]), sort_keys=True))
        score = escape(json.dumps(_thaw(chapter["score_proof"]), sort_keys=True))
        roles = escape(", ".join(chapter["public_roles"]["identities"]))
        faults = escape(", ".join(chapter["faults"]["events"]) or "none recorded")
        chapters.append(f'<section aria-labelledby="match-{chapter["match_number"]}"><h2 id="match-{chapter["match_number"]}">{escape(chapter["heading"])}</h2><p>Winner: {escape(chapter["outcome"]["winner"])}</p><p>Decisive rule: {escape(chapter["outcome"]["decisive_rule"])}</p><p>Blue Brief: {escape(chapter["blue_brief"]["family"])}</p><p>Public Competitor identities: {roles}.</p><p>Declared budgets: <code>{declared}</code></p><p>Observed measurements: <code>{observed}</code></p><p>Score proof: <code>{score}</code></p><p>Fault evidence: {faults}; operational faults are separated from Competitor behavior.</p>{chapter_claims}<table><caption>Authoritative Match timeline</caption><thead><tr><th scope="col">Time</th><th scope="col">Event</th><th scope="col">Phase</th><th scope="col">Evidence ID</th></tr></thead><tbody>{rows}</tbody></table><p>Interview-to-Red comparison is observational only and does not establish intent.</p></section>')
    manifests = "".join(f"<li>{escape(item['public_name'])}: {escape(item['model_id'])}</li>" for item in model["competitor_manifests"])
    remaining_claims = "".join(rendered_claim(identifier) for identifier in claims if identifier not in visible_claim_ids)
    correction = model["corrections"]
    previous = f'<p>Supersedes: {escape(str(correction["supersedes_report_url"]))}</p>' if correction["supersedes_report_url"] else ""
    successor = f'<p>Superseded by: {escape(str(correction["superseded_by_report_url"]))}</p>' if correction["superseded_by_report_url"] else ""
    canonical_record = escape(json.dumps({key: _thaw(model[key]) for key in ("incident_appendix", "validity", "limitations", "hashes", "reproducibility_links", "corrections")}, ensure_ascii=False, sort_keys=True, indent=2))
    canonical_document = escape(json.dumps(_thaw(model["document"]), ensure_ascii=False, sort_keys=True, indent=2))
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{escape(model["title"])}</title><style>
body{{font-family:system-ui,sans-serif;line-height:1.5;max-width:72rem;margin:auto;padding:1rem;color:#111;background:#fff}} .citation{{font-family:ui-monospace,monospace}} .claim{{border-left:.3rem solid #345;padding-left:1rem;margin:1rem 0}} table{{border-collapse:collapse;width:100%}} th,td{{border:1px solid #555;padding:.35rem;text-align:left;vertical-align:top}} @media print{{body{{max-width:none;font-size:10pt}}section{{break-inside:avoid}}a{{color:#000;text-decoration:none}}}}
</style></head><body><header><p>{escape(model["scope"]["label"])}</p><h1>{escape(model["title"])}</h1><p id="scope-note">{escape(model["scope"]["repeated_scope_language"])}</p></header><main id="result-report"><section aria-labelledby="outcome" aria-describedby="scope-note"><h2 id="outcome">Outcome</h2><p>Winner: {escape(model["outcome"]["winner"])}</p><p>Decisive rule: {escape(model["outcome"]["decisive_rule"])}</p>{outcome_claims}</section><section aria-labelledby="competitors"><h2 id="competitors">Competitor manifests</h2><ul>{manifests}</ul></section><section aria-labelledby="analysis"><h2 id="analysis">Methodology and interpretation</h2>{remaining_claims}</section>{''.join(chapters)}<section aria-labelledby="validity"><h2 id="validity">Validity and reproducibility</h2><p>Validity status: {escape(model["validity"]["status"])}</p><p>Evidence checksum: {escape(model["hashes"]["checksums"]["public"])}</p><pre>{canonical_record}</pre></section><section aria-labelledby="canonical-document"><h2 id="canonical-document">Canonical semantic projection</h2><pre>{canonical_document}</pre></section><section aria-labelledby="corrections"><h2 id="corrections">Corrections and provenance</h2><p>Status: {escape(correction["status"])}</p><p>{escape(correction["visible_notice"])}</p>{previous}{successor}<p>Evidence: {escape(model["source_evidence"]["url"])}</p></section></main><footer><p>Claims are typed and cited to frozen evidence at the stated granularity.</p></footer></body></html>'''


def render_report_pdf(model: Mapping[str, Any]) -> bytes:
    """Produce a deterministic, print-ready PDF directly from the same model."""
    lines = []
    for block in model["document"]["children"]:
        if block["type"] in {"heading", "paragraph"}:
            lines.append(block["text"])
        elif block["type"] == "link":
            lines.append(f"{block['label']}: {block['url']}")
        elif block["type"] == "claim":
            lines.append(f"{block['id']} [{block['claim_type']}]: {block['text']}")
        elif block["type"] == "table":
            lines.append(block["caption"])
            lines.extend(" | ".join(row) for row in block["rows"])
        elif block["type"] == "list":
            lines.extend(block["items"])
        elif block["type"] == "incident":
            lines.append(f"Incidents: {json.dumps(_thaw(block['items']), sort_keys=True)}")
    lines.append(f"Decisive rule: {model['outcome']['decisive_rule']}")
    lines.append("Competitor manifests:")
    lines.extend(f"{competitor['public_name']}: {competitor['model_id']}" for competitor in model["competitor_manifests"])
    lines.append(f"Protocol: {json.dumps(_thaw(model['protocol']), sort_keys=True)}")
    lines.append(f"Score proof: {json.dumps(_thaw(model['score_proof']), sort_keys=True)}")
    for claim in model["claims"]:
        lines.append(f"{claim['id']} [{claim['type']}]: {claim['text']}")
    for chapter in model["technical_chapters"]:
        lines.append(chapter["heading"])
        lines.append(f"Evidence: {chapter['outcome']['evidence_event_id']}")
        lines.append(f"Blue Brief: {chapter['blue_brief']['family']}")
        lines.append(f"Budgets: {json.dumps(_thaw(chapter['budgets']), sort_keys=True)}")
        lines.append(f"Match score proof: {json.dumps(_thaw(chapter['score_proof']), sort_keys=True)}")
        lines.extend(f"Timeline: {event['event_id']} {event['event_type']}" for event in chapter["timeline"])
    lines.append(f"Validity: {model['validity']['status']}")
    lines.append(f"Evidence checksum: {model['hashes']['checksums']['public']}")
    lines.append(model["corrections"]["visible_notice"])
    wrapped = [part for line in lines for part in (textwrap.wrap(str(line), width=82, replace_whitespace=False, drop_whitespace=False) or [""])]
    escaped = [line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)").encode("ascii", "replace").decode("ascii") for line in wrapped]
    page_lines = [escaped[index : index + 52] for index in range(0, len(escaped), 52)]
    font_object = 3 + (2 * len(page_lines))
    info_object = font_object + 1
    structure_object = info_object + 1
    parent_tree_object = structure_object + 1
    first_structure_element = parent_tree_object + 1
    objects: list[str] = [
        f"<< /Type /Catalog /Pages 2 0 R /Lang (en-US) /MarkInfo << /Marked true >> /StructTreeRoot {structure_object} 0 R >>",
        "",  # The Pages object is filled once every Page object has an ID.
    ]
    page_object_ids: list[int] = []
    for index, page in enumerate(page_lines):
        page_object = 3 + (2 * index)
        stream_object = page_object + 1
        page_object_ids.append(page_object)
        stream = "BT /F1 10 Tf 54 790 Td " + " ".join(f"({line}) Tj 0 -13 Td" for line in page) + " ET"
        objects.extend([
            f"<< /Type /Page /Parent 2 0 R /StructParents {index} /MediaBox [0 0 595 842] /Resources << /Font << /F1 {font_object} 0 R >> >> /Contents {stream_object} 0 R >>",
            f"<< /Length {len((' /P <</MCID 0>> BDC ' + stream + ' EMC').encode('ascii'))} >>\nstream\n/P <</MCID 0>> BDC {stream} EMC\nendstream",
        ])
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(f'{item} 0 R' for item in page_object_ids)}] /Count {len(page_object_ids)} >>"
    objects.extend([
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Title ({escaped[0]}) /Subject ({escaped[1]}) /Lang (en-US) >>",
        f"<< /Type /StructTreeRoot /K [{' '.join(f'{first_structure_element + index} 0 R' for index in range(len(page_lines)))}] /ParentTree {parent_tree_object} 0 R >>",
        f"<< /Nums [{' '.join(f'{index} [{first_structure_element + index} 0 R]' for index in range(len(page_lines)))}] >>",
    ])
    objects.extend(
        f"<< /Type /StructElem /S /Document /P {structure_object} 0 R /Pg {page_object_ids[index]} 0 R /K 0 >>"
        for index in range(len(page_lines))
    )
    document = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(document))
        document.extend(f"{number} 0 obj\n{obj}\nendobj\n".encode("ascii"))
    xref = len(document)
    document.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("ascii"))
    document.extend("".join(f"{offset:010d} 00000 n \n" for offset in offsets[1:]).encode("ascii"))
    document.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R /Info {info_object} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii"))
    return bytes(document)


@dataclass(frozen=True)
class ResultReport:
    """The synchronized outputs derived from one canonical report model."""

    model: ReportModel
    json: str
    html: str
    pdf: bytes


generate_result_report = build_result_report


__all__ = ["ReportModel", "ResultReport", "ResultReportError", "build_result_report", "generate_result_report", "render_report_html", "render_report_pdf"]
