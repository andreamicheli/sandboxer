"""LLM-drafted narrative layer for the Result Report (LaTeX + site subpage).

The canonical Result Report (``report.py``) is deterministic: winner, scores,
budget accounting, event IDs and hashes are derived mechanically from the frozen
Evidence Bundle.  This module adds the *prose* layer on top of that model — a
text model drafts a summary, one short narrative per Match, a cross-Match
analysis, and a limitations paragraph, grounded to the deterministic facts.

A deterministic template fallback keeps the pipeline running without a model or
credentials, and validation fails closed, so drafted prose can never be
mistaken for fact: the renderers always print the deterministic outcome and
numbers next to the drafted narrative.
"""

from __future__ import annotations

import json
import re
from html import escape
from typing import Any, Mapping, Protocol, Sequence

from .agents import HeadlessAgentAdapter, phase_adapter


class ReportNarrativeError(ValueError):
    pass


# Defense in depth: the report model is already public/redacted, but a drafted
# paragraph must still never contain credential-looking material.
_CREDENTIAL = re.compile(
    r"(?:sk|pk|ghp|xox[baprs])-[A-Za-z0-9_-]{8,}|AIza[A-Za-z0-9_-]{20,}|Bearer\s+[A-Za-z0-9._-]{8,}",
    re.I,
)


def validate_report_narrative(
    narrative: Mapping[str, Any], model: Mapping[str, Any]
) -> tuple[str, ...]:
    """Return human-readable failures (empty when the narrative is valid)."""
    failures: list[str] = []
    if not isinstance(narrative, Mapping):
        return ("narrative must be an object",)
    for section in ("summary", "analysis", "limitations"):
        if not str(narrative.get(section, "")).strip():
            failures.append(f"{section}: empty")
    match_numbers = {int(chapter["match_number"]) for chapter in model["technical_chapters"]}
    per_match = narrative.get("per_match")
    if not isinstance(per_match, list) or not per_match:
        failures.append("per_match: must be a non-empty list")
    else:
        seen: set[int] = set()
        for index, item in enumerate(per_match):
            if not isinstance(item, Mapping):
                failures.append(f"per_match[{index}]: not an object")
                continue
            number = item.get("match_number")
            if not isinstance(number, int) or isinstance(number, bool) or number not in match_numbers:
                failures.append(f"per_match[{index}]: bad match_number {number!r}")
            elif number in seen:
                failures.append(f"per_match[{index}]: duplicate match_number")
            seen.add(number)
            if not str(item.get("narrative", "")).strip():
                failures.append(f"per_match[{index}]: empty narrative")
    for key in ("summary", "analysis", "limitations"):
        if _CREDENTIAL.search(str(narrative.get(key, ""))):
            failures.append(f"{key}: possible credential leak")
    for item in per_match if isinstance(per_match, list) else ():
        if isinstance(item, Mapping) and _CREDENTIAL.search(str(item.get("narrative", ""))):
            failures.append("per_match: possible credential leak")
    return tuple(failures)


class ReportNarrativeDrafter(Protocol):
    """Produces a grounded narrative from the deterministic report model."""

    def draft(self, model: Mapping[str, Any]) -> dict[str, Any]: ...


class DeterministicReportNarrativeDrafter:
    """Template prose derived from the deterministic claims (no model)."""

    def draft(self, model: Mapping[str, Any]) -> dict[str, Any]:
        winner = str(model["outcome"]["winner"])
        decisive = str(model["outcome"]["decisive_rule"])
        competitors = [str(item["public_name"]) for item in model["competitor_manifests"]]
        others = [name for name in competitors if name != winner] or ["its competitor"]
        summary = (
            f"{winner} won this experimental simulated-CTF Series against "
            f"{' and '.join(others)} under the {decisive} rule. Sandboxer is a "
            "synthetic-only benchmark; this outcome is scoped to the recorded configuration."
        )
        per_match: list[dict[str, Any]] = []
        for chapter in model["technical_chapters"]:
            per_match.append(
                {
                    "match_number": int(chapter["match_number"]),
                    "narrative": (
                        f"Match {chapter['match_number']} finished with "
                        f"{chapter['outcome']['winner']} under "
                        f"{chapter['outcome']['decisive_rule']}, following the "
                        f"{chapter['blue_brief']['family']} Blue Brief."
                    ),
                }
            )
        analysis = (
            "Both declared Competitors used the same recorded protocol and competitive "
            "budget categories. This comparison holds only for this configuration and does "
            "not establish an intrinsic model personality or general cyber capability."
        )
        limitations = (
            "The sample is small and targets are synthetic-only; provider and adapter "
            "behaviour, Blue Brief families, and budget ceilings all bound generalization. "
            "Additional independently frozen Series are required before broader claims."
        )
        return {"summary": summary, "per_match": per_match, "analysis": analysis, "limitations": limitations}


class HeadlessReportNarrativeDrafter:
    """Drafts the narrative with a headless coding agent (default ``codex``)."""

    def __init__(self, *, adapter: HeadlessAgentAdapter | None = None) -> None:
        self._adapter = adapter or phase_adapter("report_narrative")

    def draft(self, model: Mapping[str, Any]) -> dict[str, Any]:
        text = self._adapter.complete(self._prompt(model))
        return self._parse(text, model)

    def _prompt(self, model: Mapping[str, Any]) -> str:
        chapters = [
            {
                "match_number": chapter["match_number"],
                "winner": chapter["outcome"]["winner"],
                "rule": chapter["outcome"]["decisive_rule"],
                "blue_brief": chapter["blue_brief"]["family"],
                "observed": chapter["budgets"]["observed_measurements"],
            }
            for chapter in model["technical_chapters"]
        ]
        claims = [
            {"id": claim["id"], "type": claim["type"], "text": claim["text"]}
            for claim in model["claims"]
            if isinstance(claim, Mapping)
        ]
        return json.dumps(
            {
                "task": (
                    "Write a short, honest narrative for a public result report of an "
                    "experimental simulated capture-the-flag benchmark. The facts below are "
                    "deterministic and authoritative — restate them faithfully, never invent "
                    "numbers, winners, or capabilities. Keep it readable, curious, and "
                    "appropriately hedged; this is a simulated environment, not a real "
                    "cybersecurity capability measure."
                ),
                "rules": {
                    "grounding": "only restate facts given below; do not add external claims",
                    "tone": "clear, measured, no hype, no anthropomorphizing the models",
                    "structure": {
                        "summary": "one short paragraph (winner, decisive rule, scope)",
                        "per_match": "one entry per match_number with a 2-3 sentence narrative",
                        "analysis": "one short cross-Match discussion paragraph",
                        "limitations": "one short limitations paragraph",
                    },
                },
                "outcome": {
                    "winner": model["outcome"]["winner"],
                    "decisive_rule": model["outcome"]["decisive_rule"],
                },
                "competitors": [item["public_name"] for item in model["competitor_manifests"]],
                "claims": claims,
                "matches": chapters,
                "output_schema": {
                    "summary": "string",
                    "per_match": [{"match_number": "int", "narrative": "string"}],
                    "analysis": "string",
                    "limitations": "string",
                },
            },
            indent=2,
        )

    def _parse(self, text: str, model: Mapping[str, Any]) -> dict[str, Any]:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```", 2)[1]
            if cleaned.lstrip().startswith("json"):
                cleaned = cleaned.lstrip()[4:]
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise ReportNarrativeError("REPORT_NARRATIVE_PARSE_FAILED")
        try:
            payload = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as error:
            raise ReportNarrativeError("REPORT_NARRATIVE_PARSE_FAILED") from error
        if not isinstance(payload, dict):
            raise ReportNarrativeError("REPORT_NARRATIVE_EMPTY")
        # Normalize match_number to int before validation (models may emit floats/strings).
        if isinstance(payload.get("per_match"), list):
            for item in payload["per_match"]:
                if not isinstance(item, dict):
                    continue
                number = item.get("match_number")
                if isinstance(number, float) and number.is_integer():
                    item["match_number"] = int(number)
                elif isinstance(number, str) and number.strip().lstrip("-").isdigit():
                    item["match_number"] = int(number.strip())
        failures = validate_report_narrative(payload, model)
        if failures:
            raise ReportNarrativeError(f"REPORT_NARRATIVE_INVALID: {'; '.join(failures)}")
        return dict(payload)


def draft_report_narrative(
    model: Mapping[str, Any],
    *,
    drafter: ReportNarrativeDrafter | None = None,
) -> dict[str, Any]:
    """Draft and validate a narrative; raises if the draft is invalid."""
    narrative = (drafter or DeterministicReportNarrativeDrafter()).draft(model)
    failures = validate_report_narrative(narrative, model)
    if failures:
        raise ReportNarrativeError(f"REPORT_NARRATIVE_INVALID: {'; '.join(failures)}")
    return dict(narrative)


def render_narrative_html(model: Mapping[str, Any], narrative: Mapping[str, Any]) -> str:
    """Render the narrative as a self-contained, less-detailed site subpage.

    Deterministic facts (winner, decisive rule, per-Match result, disclaimer)
    are printed from the report model; the drafted prose sits alongside them and
    links point back to the canonical report and the detailed PDFs.
    """
    winner = escape(str(model["outcome"]["winner"]))
    decisive = escape(str(model["outcome"]["decisive_rule"]))
    competitors = ", ".join(escape(str(item["public_name"])) for item in model["competitor_manifests"])
    scope = escape(str(model["scope"]["repeated_scope_language"]))
    title = escape(str(model["title"]))
    summary = escape(str(narrative["summary"]))
    analysis = escape(str(narrative["analysis"]))
    limitations = escape(str(narrative["limitations"]))
    by_number = {int(chapter["match_number"]): chapter for chapter in model["technical_chapters"]}
    per_match_blocks: list[str] = []
    for item in narrative["per_match"]:
        number = int(item["match_number"])
        chapter = by_number[number]
        per_match_blocks.append(
            "<section class=\"match\">"
            f"<h3>Match {number}</h3>"
            f"<p class=\"outcome\">Result: {escape(str(chapter['outcome']['winner']))}"
            f" &mdash; {escape(str(chapter['outcome']['decisive_rule']))}.</p>"
            f"<p>{escape(str(item['narrative']))}</p>"
            "</section>"
        )
    per_match_html = "\n".join(per_match_blocks)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title><style>
body{{font-family:system-ui,sans-serif;line-height:1.55;max-width:46rem;margin:auto;padding:1.5rem;color:#111;background:#fff}}
h1{{font-size:1.6rem}} .outcome{{font-weight:600;color:#345}} .eyebrow{{text-transform:uppercase;letter-spacing:.12em;font-size:.8rem;color:#666}}
nav.links{{margin:1.5rem 0}} nav.links a{{display:inline-block;margin-right:1rem;color:#1a4fd6}}
footer{{margin-top:2.5rem;padding-top:1rem;border-top:1px solid #ddd;font-size:.85rem;color:#555}}
</style></head><body>
<header><p class="eyebrow">Sandboxer &middot; experimental benchmark &middot; simulated CTF Arena</p>
<h1>{title}</h1><p>{scope}</p></header>
<main>
<section aria-labelledby="summary"><h2 id="summary">Summary</h2><p>{summary}</p></section>
<section aria-labelledby="outcome"><h2 id="outcome">Outcome</h2>
<p><strong>Winner:</strong> {winner}</p><p><strong>Decisive rule:</strong> {decisive}</p>
<p><strong>Competitors:</strong> {competitors}.</p></section>
<section aria-labelledby="matches"><h2 id="matches">Matches</h2>{per_match_html}</section>
<section aria-labelledby="analysis"><h2 id="analysis">Analysis</h2><p>{analysis}</p></section>
<section aria-labelledby="limitations"><h2 id="limitations">Limitations</h2><p>{limitations}</p></section>
<nav class="links" aria-label="Full report"><a href="report.html">Full canonical report</a>
<a href="report-detailed.pdf">Detailed report (PDF)</a>
<a href="narrative.pdf">Narrative report (PDF)</a>
<a href="evidence.json">Frozen evidence</a></nav>
</main>
<footer><p>Facts and numbers are derived mechanically from frozen, signed evidence. The narrative prose is drafted by a model and grounded to that evidence; it is interpretation, not additional evidence.</p></footer>
</body></html>"""


__all__ = [
    "DeterministicReportNarrativeDrafter",
    "HeadlessReportNarrativeDrafter",
    "ReportNarrativeDrafter",
    "ReportNarrativeError",
    "draft_report_narrative",
    "render_narrative_html",
    "validate_report_narrative",
]
