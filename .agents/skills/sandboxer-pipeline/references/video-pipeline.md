# Video pipeline layers and 2026-08-22 fix series

Three layers; fixes must respect layer boundaries. When delegating to opencode,
group tasks by LAYER (data / manifest / renderer), not by user-visible symptom:
the same TSX file is touched by several symptoms, so parallel runs on one
worktree collide. Batch sequentially whenever files are shared.

## Layer 1: Manifest builder — `pilot/sandboxer_v0/video.py`

- Scenes, commentary budgets (`_scene_budgets`), phase segmentation.
- Match scenes segment by telemetry phase: blue match → `interview_card` (4s)
  → `interviews` (8–45s, payload from INTERVIEW_RECORDED frames) →
  `red_phase_card` (3s) → red continuation match scene. Replays without a
  phase keep one uncut scene.
- Match commentary budget is DURATION-SCALED: `max_lines =
  clamp(round(duration_s/12), 6, 40)`, `max_total_words = max_lines * 20`,
  `max_words_per_line = 25`. A previous fixed cap of 10 lines silenced the
  second half of long matches — never reintroduce fixed caps for long scenes.
- Downstream consistency keys: per-scene packing keys (`match:1`,
  `match:1:2`, `interviews:1`) shared by `scene_starts`, budgets, `_locate`;
  `_anchor` maps frames through explicit ns windows. YouTube chapter labels
  for scene types live in `youtube_metadata` (`pilot/sandboxer_v0/youtube.py`).

## Layer 2: Renderer — `video/src/index.tsx`, `video/src/arena.tsx`

- Accent colors come ONLY from `accentOf(identity, fallback)` in
  `video/src/logos.ts` (brand registry accents: poolside #4137ff,
  meta #4f8ef7). The hardcoded ACCENTS map was removed deliberately;
  never reintroduce one. Fallbacks: COBALT/AMBER for unknown identities.
- BenchmarkBars grow outward from a shared center axis (left bar leftward from
  50%, right bar rightward).
- Arena (`arena.tsx`): defenses lay out per side from ~140px off the center
  line toward the avatar home at 150px pitch; global deterministic
  `fitScale = min(1, 780 / max(spanNeeded_left, spanNeeded_right))` applied as
  CSS transform scale on all arena content (origin canvas center). Shape base
  shrinks with fitScale. Win-probability bar stays OUTSIDE the scaled wrapper.
  `targetPosition` uses the same layout so projectiles still hit targets.

## Layer 3: Data — `pilot/data/benchmark_snapshot.json`

- Schema `sandboxer.benchmark-snapshot.v1`, keyed by pair slug
  ("model-a vs model-b"), either order matches via slug normalization.
- DUAL-PUBLISHED RULE: a benchmark row exists only if BOTH models have
  published scores on that benchmark/harness. Never estimate missing values.
- Cross-version harness caveats go in `harness_note` (e.g. TB 2.0-Terminus vs
  2.1, vendor-reported vs independent runs).
- `build_real_artifacts.py --benchmark-data <path>` loads it;
  missing/invalid file raises clean `BenchmarkDataError`; unknown pair →
  empty snapshot without crashing. Tests cover byte-identical selection for
  the laguna-vs-muse pair plus well-formedness sweep over all 15 pairs.
- Research doc with sources (Artificial Analysis, poolside.ai, vendor cards):
  `docs/benchmark-dataset-series001.md`. Roster assumption: round-robin over
  Laguna S 2.1, Muse Spark 1.2, Tencent Hy3, DeepSeek V4 Flash/Pro,
  Step 3.7 Flash, Qwen 3.7 Max.

## Verification

- Python: `cd pilot && .venv/bin/python -m pytest tests/test_video.py
  tests/test_build_real_artifacts.py tests/test_arena_visual.py -q`
- TS: `cd video && npm run typecheck`
- Full suite before declaring done (~605 tests); known pre-existing failure:
  `test_setup_credentials.py::test_orca_rejects_missing_key` (fails on clean
  HEAD too).

## Commits (2026-08-22)

- `6446666` feat(benchmarks): dual-published snapshot dataset for all Series 001 pairs
- `cd50233` feat(video): phase-segmented match scenes with scaled commentary budgets
- `c944d17` feat(render): brand accents, center-origin bars, adaptive arena layout

Pushed to `origin/agent/sandboxer-v0-backup`.
