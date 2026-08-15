#!/usr/bin/env python3
"""Build the downloadable LaTeX (PDF) view of a Series Result Report.

Consumes a frozen Evidence Bundle (JSON) and produces, in ``--out-dir``:

- ``report.tex``  — the LaTeX source (detailed analysis + data tables);
- ``charts/*.png`` — matplotlib charts (tokens, tool calls, turns, timeline);
- ``report.pdf``  — the compiled PDF (with ``--compile``, the default).

With no ``--bundle`` a deterministic fake-fixture bundle is built so the full
path can be exercised without a real Series.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PILOT_ROOT = Path(__file__).resolve().parents[1]
if str(PILOT_ROOT) not in sys.path:
    sys.path.insert(0, str(PILOT_ROOT))

from sandboxer_v0 import (
    ControlledCompetitor,
    FakeModelAdapter,
    FakeRunnerBackend,
    MatchPolicy,
    SeriesSpec,
    execute_series,
)
from sandboxer_v0.report_latex import build_report_latex


def _demo_bundle() -> dict:
    spec = SeriesSpec(
        schema_version="sandboxer.series-spec.v1",
        series_id="report-pdf-demo",
        seed="report-pdf-demo-seed",
        competitors=(
            ControlledCompetitor("Laguna S 2.1", FakeModelAdapter("poolside/laguna-s-2.1-free", ("defend", "capture"))),
            ControlledCompetitor("Muse Spark 1.2", FakeModelAdapter("meta/muse-spark-1.2-contributor", ("defend", "finish"))),
        ),
        match_policy=MatchPolicy(best_of=3, output_token_budget=100, turn_budget=2, tool_budget=2),
        runner_backend=FakeRunnerBackend(),
    )
    return execute_series(spec).evidence_bundle


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, default=None, help="frozen evidence bundle JSON")
    parser.add_argument("--out-dir", type=Path, default=Path("artifacts/report-pdf"))
    parser.add_argument("--compile", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args(argv)

    bundle: object
    if args.bundle is not None:
        bundle = json.loads(args.bundle.read_text(encoding="utf-8"))
    else:
        print("no --bundle given; building a deterministic demo fixture", file=sys.stderr)
        bundle = _demo_bundle()

    report = build_report_latex(bundle, compile_pdf=args.compile)

    out = args.out_dir
    charts_dir = out / "charts"
    charts_dir.mkdir(parents=True, exist_ok=True)
    (out / "report.tex").write_text(report.tex, encoding="utf-8")
    for name, content in report.charts.items():
        (charts_dir / name).write_bytes(content)
    if report.pdf:
        (out / "report.pdf").write_bytes(report.pdf)
        print(f"wrote {out / 'report.pdf'} ({len(report.pdf)} bytes)")
    print(f"wrote {out / 'report.tex'} and {len(report.charts)} chart(s) under {charts_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
