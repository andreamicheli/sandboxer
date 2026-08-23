'use strict';

/*
 * Pure arena geometry: places N defense nodes inside the strip between the
 * two avatar/logo exclusion zones.  No React, no DOM, no randomness -- the
 * same inputs always produce the same positions, every returned node keeps
 * MIN_GAP clearance from its neighbours, and no node ever intersects a
 * logo box (each box is additionally padded by LOGO_CLEARANCE).
 */

const BASE_NODE_SIZE = 96;
const MIN_GAP = 18;
const MARGIN_X = 16;
const MARGIN_Y = 36;
const LOGO_CLEARANCE = 28;
const COLLISION_PAD = 2;
const SHRINK_FACTOR = 0.92;
const MIN_NODE_SIZE = 10;

function inflateBox(box, pad) {
  return { x: box.x - pad, y: box.y - pad, w: box.w + 2 * pad, h: box.h + 2 * pad };
}

function circleHitsRect(px, py, radius, rect) {
  const nx = Math.min(Math.max(px, rect.x), rect.x + rect.w);
  const ny = Math.min(Math.max(py, rect.y), rect.y + rect.h);
  const dx = px - nx;
  const dy = py - ny;
  return dx * dx + dy * dy < radius * radius;
}

function corridor(width, height, boxes) {
  let left = MARGIN_X;
  let right = width - MARGIN_X;
  const midX = width / 2;
  for (const box of boxes) {
    if (box.x + box.w <= midX) {
      left = Math.max(left, box.x + box.w);
    } else if (box.x >= midX) {
      right = Math.min(right, box.x);
    } else {
      return null;
    }
  }
  if (!(right - left >= MIN_NODE_SIZE)) return null;
  if (!(height - 2 * MARGIN_Y >= MIN_NODE_SIZE)) return null;
  return { left, right, top: MARGIN_Y, bottom: height - MARGIN_Y };
}

function tryLayout(ids, width, boxes, band, nodeSize) {
  const radius = nodeSize / 2 + COLLISION_PAD;
  const pitch = nodeSize + MIN_GAP;
  const usableH = band.bottom - band.top;
  const rowsMax = Math.floor((usableH - 2 * radius) / pitch) + 1;
  const innerW = band.right - band.left - 2 * radius;
  const colsMax = Math.floor(innerW / pitch) + 1;
  if (rowsMax < 1 || colsMax < 1) return null;
  const rows = Math.ceil(ids.length / colsMax);
  if (rows > rowsMax) return null;

  const centerY = (band.top + band.bottom) / 2;
  const rowY = (row) => centerY + (row - (rows - 1) / 2) * pitch;
  const visitOrder = [];
  if (rows % 2 === 1) {
    const mid = (rows - 1) / 2;
    visitOrder.push(mid);
    for (let d = 1; d <= mid; d++) visitOrder.push(mid - d, mid + d);
  } else {
    const half = rows / 2;
    for (let d = 0; d < half; d++) visitOrder.push(half - 1 - d, half + d);
  }

  const candidates = [];
  for (let x = band.left + radius; x <= band.right - radius + 1e-9; x += pitch) {
    for (const row of visitOrder) {
      candidates.push({ x, y: rowY(row) });
    }
  }

  const positions = {};
  let next = 0;
  for (const point of candidates) {
    if (next >= ids.length) break;
    if (point.x < MARGIN_X || point.x > width - MARGIN_X) continue;
    if (point.y - radius < band.top || point.y + radius > band.bottom) continue;
    if (boxes.some((rect) => circleHitsRect(point.x, point.y, radius, rect))) continue;
    positions[ids[next]] = { x: point.x, y: point.y };
    next++;
  }
  if (next < ids.length) return null;

  return { rows, nodeSize, positions };
}

function computeDefenseLayout(defenses, width, height, logoBoxes) {
  const ids = (defenses ?? []).map((d) => d.id).filter((id) => typeof id === 'string');
  if (!Number.isFinite(width) || !Number.isFinite(height) || ids.length === 0) {
    return { positions: {}, nodeSize: BASE_NODE_SIZE, rows: 0, minGap: MIN_GAP };
  }

  const inflated = (logoBoxes ?? [])
    .filter((b) => [b.x, b.y, b.w, b.h].every(Number.isFinite) && b.w > 0 && b.h > 0)
    .map((b) => inflateBox(b, LOGO_CLEARANCE));

  for (let nodeSize = BASE_NODE_SIZE; ; ) {
    const cappedSize = Math.max(MIN_NODE_SIZE, nodeSize);
    const band =
      corridor(width, height, inflated) ?? {
        left: MARGIN_X,
        right: width - MARGIN_X,
        top: MARGIN_Y,
        bottom: height - MARGIN_Y,
      };
    const result = tryLayout(ids, width, inflated, band, cappedSize);
    if (result) return { ...result, minGap: MIN_GAP };
    if (cappedSize <= MIN_NODE_SIZE) throw new Error('ARENA_LAYOUT_OVERFLOW');
    nodeSize = cappedSize * SHRINK_FACTOR;
  }
}

module.exports = { computeDefenseLayout, BASE_NODE_SIZE, MIN_GAP };
module.exports.default = computeDefenseLayout;
