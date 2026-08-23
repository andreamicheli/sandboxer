"""Geometry invariants for video/src/arena-layout.js, exercised through node."""

from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path

import pytest

VIDEO_DIR = Path(__file__).resolve().parents[2] / "video"
MODULE_PATH = VIDEO_DIR / "src" / "arena-layout.js"

HARNESS = """
const mod = require(process.argv[1]);
const spec = JSON.parse(process.argv[2]);
const result = mod.computeDefenseLayout(spec.defenses, spec.width, spec.height, spec.logoBoxes);
process.stdout.write(JSON.stringify(result));
"""

CANVAS_W = 1920
STRIP_H = 280
WIN_BAR_H = 48
USABLE_H = STRIP_H - WIN_BAR_H


def _logo_boxes():
    mid_y = USABLE_H / 2
    return [
        {"x": home_x - 64, "y": mid_y - 170 * 0.38, "w": 128, "h": 170}
        for home_x in (180, 1740)
    ]


def _defenses(n):
    return [{"id": f"d{i}", "competitor": "L" if i % 2 == 0 else "R"} for i in range(n)]


def _layout(defenses, width=CANVAS_W, height=USABLE_H, logo_boxes=None):
    spec = json.dumps(
        {
            "defenses": defenses,
            "width": width,
            "height": height,
            "logoBoxes": _logo_boxes() if logo_boxes is None else logo_boxes,
        }
    )
    proc = subprocess.run(
        ["node", "-e", HARNESS, str(MODULE_PATH), spec],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout)


def _hits_box(pos, radius, box):
    nx = min(max(pos["x"], box["x"]), box["x"] + box["w"])
    ny = min(max(pos["y"], box["y"]), box["y"] + box["h"])
    return math.hypot(pos["x"] - nx, pos["y"] - ny) < radius


def test_thirteen_defenses_never_intersect_either_logo_box():
    layout = _layout(_defenses(13))
    assert len(layout["positions"]) == 13
    radius = layout["nodeSize"] / 2
    for defense_id, pos in layout["positions"].items():
        for box in _logo_boxes():
            assert not _hits_box(pos, radius, box), f"{defense_id} intersects logo box"


@pytest.mark.parametrize("count", [5, 13, 26])
def test_pairwise_min_gap_respected(count):
    layout = _layout(_defenses(count))
    positions = list(layout["positions"].values())
    assert len(positions) == count
    min_center_dist = layout["nodeSize"] + layout["minGap"]
    for i, left in enumerate(positions):
        for right in positions[i + 1 :]:
            dist = math.hypot(left["x"] - right["x"], left["y"] - right["y"])
            assert dist >= min_center_dist - 1e-6, f"nodes {i} too close ({dist} < {min_center_dist})"


def test_output_is_deterministic():
    first = _layout(_defenses(13))
    second = _layout(_defenses(13))
    assert first == second


def test_positions_stay_inside_the_strip_and_match_ids():
    defenses = _defenses(13)
    layout = _layout(defenses)
    assert set(layout["positions"]) == {d["id"] for d in defenses}
    for pos in layout["positions"].values():
        assert 0 <= pos["x"] <= CANVAS_W and 0 <= pos["y"] <= USABLE_H
        assert math.isfinite(pos["x"]) and math.isfinite(pos["y"])


def test_empty_defense_list_yields_no_positions():
    layout = _layout([])
    assert layout["positions"] == {}
