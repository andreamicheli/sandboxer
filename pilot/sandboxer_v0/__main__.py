"""Run a credential-free controlled fixture at the public dry-run seam."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json

from .series import (
    ControlledCompetitor,
    FakeModelAdapter,
    FakeRunnerBackend,
    MatchPolicy,
    SeriesSpec,
    execute_series,
)


def _fixture(name: str) -> SeriesSpec:
    teardown = "uncertain" if name == "teardown-uncertain" else "destroy"
    return SeriesSpec(
        schema_version="sandboxer.series-spec.v1",
        series_id=f"dry-run-{name}",
        seed="public-fixture-v1",
        competitors=(
            ControlledCompetitor("atlas", FakeModelAdapter("fake/atlas", ("defend", "capture"))),
            ControlledCompetitor("borealis", FakeModelAdapter("fake/borealis", ("defend", "finish"))),
        ),
        match_policy=MatchPolicy(best_of=3, output_token_budget=100, turn_budget=2, tool_budget=2),
        runner_backend=FakeRunnerBackend(teardown=teardown),
        command_code_credit_allowance=10 if name == "credit-pressure" else None,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture",
        choices=("valid", "teardown-uncertain", "credit-pressure"),
        default="valid",
    )
    args = parser.parse_args()
    bundle = execute_series(_fixture(args.fixture))
    print(json.dumps(asdict(bundle), sort_keys=True))
    return 0 if bundle.publication_eligible else 2


if __name__ == "__main__":
    raise SystemExit(main())
