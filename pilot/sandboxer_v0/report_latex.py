"""Downloadable LaTeX (PDF) view of a canonical Result Report, with data charts.

This module consumes the same frozen Evidence Bundle as ``report.py`` and adds
the two pieces the accessible HTML view deliberately omits for brevity:

1. **Detailed per-Match analysis** rendered as LaTeX tables (per-competitor,
   per-phase token / turn / tool accounting, Blue Brief, outcome, score proof).
2. **Data charts** generated with matplotlib and embedded via ``\\includegraphics``
   so a reader can see, not just read, what happened.

The LaTeX source and the compiled PDF are both exposed so a caller can publish
either; compilation is opt-in so deterministic tests never require a LaTeX
toolchain.
"""

from __future__ import annotations

import io
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from .report import build_result_report, ResultReport  # noqa: E402

_PHASE_COLORS = {
    "blue": "#4d9cff",
    "interview": "#9a65e8",
    "red": "#ff4d5e",
    "finalizing": "#f4a825",
    "series": "#8a97ad",
}


def _tex_escape(value: object) -> str:
    text = str(value if value is not None else "")
    return (
        text.replace("\\", r"\textbackslash{}")
        .replace("&", r"\&")
        .replace("%", r"\%")
        .replace("$", r"\$")
        .replace("#", r"\#")
        .replace("_", r"\_")
        .replace("{", r"\{")
        .replace("}", r"\}")
        .replace("~", r"\textasciitilde{}")
        .replace("^", r"\textasciicircum{}")
    )


def _as_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def compute_analytics(model: Mapping[str, Any]) -> dict[str, Any]:
    """Derive chartable measurements from the canonical report model."""
    competitors = [item["public_name"] for item in model["competitor_manifests"]]
    matches: list[dict[str, Any]] = []
    for chapter in model["technical_chapters"]:
        accounting = chapter["budgets"]["per_competitor_phase_accounting"]
        per_competitor: dict[str, dict[str, int]] = {}
        for competitor in competitors:
            rows = [row for row in accounting if row.get("competitor") == competitor]
            per_competitor[competitor] = {
                "output_tokens": sum(_as_int(row.get("output_tokens")) for row in rows),
                "turns": sum(_as_int(row.get("turns")) for row in rows),
                "tool_calls": sum(_as_int(row.get("tool_calls")) for row in rows),
            }
        timeline: list[dict[str, Any]] = []
        for event in chapter["timeline"]:
            monotonic = event.get("monotonic_ns")
            timeline.append(
                {
                    "event_id": event.get("event_id"),
                    "event_type": event.get("event_type"),
                    "phase": event.get("phase"),
                    "monotonic_ns": monotonic if isinstance(monotonic, int) else None,
                }
            )
        matches.append(
            {
                "match_number": chapter["match_number"],
                "winner": chapter["outcome"]["winner"],
                "rule": chapter["outcome"]["decisive_rule"],
                "blue_brief": chapter["blue_brief"],
                "per_competitor": per_competitor,
                "score_proof": chapter["score_proof"],
                "timeline": timeline,
            }
        )
    return {"competitors": competitors, "matches": matches}


def _png_bytes(fig: Any) -> bytes:
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    return buffer.getvalue()


def render_charts(analytics: Mapping[str, Any]) -> dict[str, bytes]:
    """Render the data charts as PNG bytes keyed by filename."""
    competitors = list(analytics["competitors"])
    matches = list(analytics["matches"])
    match_labels = [f"Match {m['match_number']}" for m in matches]
    x = range(len(matches))
    width = 0.35
    charts: dict[str, bytes] = {}

    def _grouped(values_for_competitor: Sequence[Sequence[int]], ylabel: str, title: str) -> None:
        fig, ax = plt.subplots(figsize=(7.2, 3.4))
        for index, competitor in enumerate(competitors):
            values = [values_for_competitor[index][match_index] for match_index in x]
            ax.bar([xi + (index - 0.5) * width for xi in x], values, width, label=competitor)
        ax.set_xticks(list(x))
        ax.set_xticklabels(match_labels)
        ax.set_ylabel(ylabel)
        ax.set_title(title)
        ax.legend()
        fig.tight_layout()

    # 1. Output tokens.
    _grouped(
        [[m["per_competitor"][c]["output_tokens"] for m in matches] for c in competitors],
        "Output tokens",
        "Output tokens by competitor and match",
    )
    charts["output_tokens.png"] = _png_bytes(plt.gcf())

    # 2. Tool calls.
    _grouped(
        [[m["per_competitor"][c]["tool_calls"] for m in matches] for c in competitors],
        "Tool calls",
        "Tool calls by competitor and match",
    )
    charts["tool_calls.png"] = _png_bytes(plt.gcf())

    # 3. Turns.
    _grouped(
        [[m["per_competitor"][c]["turns"] for m in matches] for c in competitors],
        "Turns",
        "Turns by competitor and match",
    )
    charts["turns.png"] = _png_bytes(plt.gcf())

    # 4. Phase timeline (Gantt-style spans per match).
    if matches:
        fig, ax = plt.subplots(figsize=(7.2, 2.2 + 0.6 * len(matches)))
        yticks: list[float] = []
        ylabels: list[str] = []
        for row_index, match in enumerate(matches):
            y = len(matches) - row_index
            spans: dict[str, list[int]] = {}
            for event in match["timeline"]:
                if event["monotonic_ns"] is None:
                    continue
                spans.setdefault(str(event["phase"]), []).append(event["monotonic_ns"])
            match_start = min((value for values in spans.values() for value in values), default=0)
            for phase, values in sorted(spans.items()):
                start = (min(values) - match_start) / 1_000_000_000
                end = (max(values) - match_start) / 1_000_000_000
                color = _PHASE_COLORS.get(phase, "#8a97ad")
                ax.barh(y, max(end - start, 0.05), left=start, height=0.5, color=color, label=phase)
            yticks.append(y)
            ylabels.append(f"Match {match['match_number']}")
        ax.set_yticks(yticks)
        ax.set_yticklabels(ylabels)
        ax.set_xlabel("Elapsed seconds (orchestrator monotonic)")
        ax.set_title("Phase activity per match")
        handles, labels = ax.get_legend_handles_labels()
        unique = {label: handle for handle, label in zip(handles, labels)}
        ax.legend(unique.values(), unique.keys(), loc="upper right", fontsize=8)
        fig.tight_layout()
        charts["phase_timeline.png"] = _png_bytes(fig)

    return charts


def _accounting_table(match: Mapping[str, Any], competitors: Sequence[str]) -> str:
    header = " & ".join(["Competitor", "Output tokens", "Turns", "Tool calls"])
    rows = []
    for competitor in competitors:
        values = match["per_competitor"][competitor]
        rows.append(
            " & ".join(
                [
                    _tex_escape(competitor),
                    str(values["output_tokens"]),
                    str(values["turns"]),
                    str(values["tool_calls"]),
                ]
            )
        )
    return "\n".join(
        [
            r"\begin{tabular}{lrrr}",
            r"\toprule",
            header + r" \\",
            r"\midrule",
            *(row + r" \\" for row in rows),
            r"\bottomrule",
            r"\end{tabular}",
        ]
    )


def render_report_latex(
    model: Mapping[str, Any], analytics: Mapping[str, Any], charts: Sequence[str]
) -> str:
    """Render the canonical model plus analytics into a standalone LaTeX document."""
    competitors = list(analytics["competitors"])
    matches = list(analytics["matches"])
    winner = _tex_escape(model["outcome"]["winner"])
    decisive = _tex_escape(model["outcome"]["decisive_rule"])
    scope = _tex_escape(model["scope"]["repeated_scope_language"])

    blocks: list[str] = [
        r"\documentclass[11pt]{article}",
        r"\usepackage[a4paper,margin=1in]{geometry}",
        r"\usepackage[T1]{fontenc}",
        r"\usepackage[utf8]{inputenc}",
        r"\usepackage{lmodern}",
        r"\usepackage{microtype}",
        r"\usepackage{booktabs}",
        r"\usepackage{graphicx}",
        r"\usepackage[hidelinks]{hyperref}",
        r"\begin{document}",
        r"\begin{center}",
        r"{\LARGE \textbf{" + _tex_escape(model["title"]) + r"}}\\[0.4em]",
        r"{\large Detailed analysis and data appendix}",
        r"\end{center}",
        r"\noindent\textit{" + scope + r"}",
        r"\section{Outcome}",
        r"\textbf{Winner:} " + winner + r"\\",
        r"\textbf{Decisive rule:} " + decisive,
    ]

    claims = [c for c in model["claims"] if isinstance(c, Mapping)]
    if claims:
        blocks.append(r"\section{Typed claims}")
        blocks.append(r"\begin{itemize}")
        for claim in claims:
            blocks.append(
                r"\item \textbf{"
                + _tex_escape(claim["type"])
                + r"} ("
                + _tex_escape(claim["id"])
                + r"): "
                + _tex_escape(claim["text"])
                + r" --- evidence "
                + _tex_escape(", ".join(claim["evidence"]["event_ids"]))
            )
        blocks.append(r"\end{itemize}")

    blocks.append(r"\section{Data analysis}")
    for chart, caption in (
        ("output_tokens.png", "Output tokens by competitor and match."),
        ("tool_calls.png", "Tool calls by competitor and match."),
        ("turns.png", "Turns by competitor and match."),
        ("phase_timeline.png", "Phase activity per match."),
    ):
        if chart in charts:
            blocks.append(r"\begin{figure}[h!]")
            blocks.append(r"\centering")
            blocks.append(r"\includegraphics[width=\textwidth]{" + chart + r"}")
            blocks.append(r"\caption{" + caption + r"}")
            blocks.append(r"\end{figure}")

    blocks.append(r"\section{Match chapters}")
    for match in matches:
        blocks.append(r"\subsection{Match " + str(match["match_number"]) + r"}")
        blocks.append(
            r"\textbf{Outcome:} "
            + _tex_escape(match["winner"])
            + r" ("
            + _tex_escape(match["rule"])
            + r")\\"
        )
        blocks.append(
            r"\textbf{Blue Brief:} "
            + _tex_escape(match["blue_brief"].get("family"))
            + r"\\[0.3em]"
        )
        blocks.append(_accounting_table(match, competitors))

    claim_by_id = {claim["id"]: claim for claim in claims}
    blocks.append(r"\section{Limitations}")
    blocks.append(r"\begin{itemize}")
    for claim_id in model["limitations"].get("claim_ids", []):
        claim = claim_by_id.get(claim_id)
        if claim is not None:
            blocks.append(r"\item " + _tex_escape(claim["text"]))
    blocks.append(r"\end{itemize}")
    blocks.append(
        r"\section{Reproducibility}\label{sec:repro}"
        + r"\noindent Evidence bundle hash: \texttt{"
        + _tex_escape(model["hashes"]["bundle_hash"])
        + r"}."
    )
    blocks.append(r"\end{document}")
    return "\n".join(blocks) + "\n"


@dataclass(frozen=True)
class LatexReport:
    """The LaTeX source, its embedded charts, and the optional compiled PDF."""

    tex: str
    charts: dict[str, bytes]
    pdf: bytes

    @property
    def compiled(self) -> bool:
        return bool(self.pdf)


def compile_latex(tex: str, charts: Mapping[str, bytes]) -> bytes:
    """Compile LaTeX plus chart PNGs to PDF using pdflatex; raise on failure."""
    with tempfile.TemporaryDirectory(prefix="sandboxer-latex-") as directory:
        root = Path(directory)
        (root / "report.tex").write_text(tex, encoding="utf-8")
        for name, content in charts.items():
            (root / name).write_bytes(content)
        for _ in range(2):
            result = subprocess.run(
                ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "report.tex"],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    "LATEX_COMPILATION_FAILED: "
                    + "\n".join(result.stdout.splitlines()[-20:])
                )
        pdf = root / "report.pdf"
        if not pdf.is_file():
            raise RuntimeError("LATEX_PDF_MISSING")
        return pdf.read_bytes()


def build_report_latex(evidence_bundle: object, *, compile_pdf: bool = False) -> LatexReport:
    """Build the LaTeX report (and optionally its compiled PDF) from frozen evidence."""
    report: ResultReport = build_result_report(evidence_bundle)
    model = report.model.to_dict()
    analytics = compute_analytics(model)
    charts = render_charts(analytics)
    tex = render_report_latex(model, analytics, list(charts.keys()))
    pdf = compile_latex(tex, charts) if compile_pdf else b""
    return LatexReport(tex=tex, charts=charts, pdf=pdf)


__all__ = [
    "LatexReport",
    "build_report_latex",
    "compute_analytics",
    "compile_latex",
    "render_charts",
    "render_report_latex",
]
