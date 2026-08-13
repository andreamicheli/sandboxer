"""Print a seeded toy-service scenario manifest as JSON."""

from __future__ import annotations

import argparse
import json

from .scenarios import build_match_manifest, build_series_manifests


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("seed", help="Recorded scenario seed")
    parser.add_argument("--series", type=int, help="Generate a non-repeating series")
    args = parser.parse_args()
    result = (
        build_series_manifests(args.seed, args.series)
        if args.series is not None
        else build_match_manifest(args.seed)
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
