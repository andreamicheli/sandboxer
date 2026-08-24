# Episode v8 — best-of-3 series, dense commentary, emotion TTS (2026-08-23)

All code merged on branch agent/sandboxer-v0-backup; full suite 695+ green
(known env failure test_orca_rejects_missing_key excluded).

## What changed vs v7

- **Series runner** `pilot/scripts/run_series.py`: plays exactly 3 matches
  (`<series-id>-m1/-m2/-m3`), per-match seeds, failed match = recorded
  datapoint, writes `<series-id>.series.json`. Aggregation in
  `sandboxer_v0/series_result.py`: first to two wins → else lowest total
  provider tokens tie-break → exact tie = winner null (fail-closed).
- **Series manifest**: `build_real_artifacts.py --series --telemetry f1 f2 f3
  --result r1 r2 r3 --series-id <id>` merges replays with cumulative frame
  offsets, `m<i>:` event-id prefixes, 2 intermission cards, one
  model_cards_and_rules up front. Single-match path byte-identical.
- **Interviews**: `interview_line` telemetry events carry verbatim final_text;
  converter emits INTERVIEW_RECORDED frames → manifest.interviews →
  interview_card + interviews scenes between blue and red (6s/entry, cap 30s).
- **Dense commentary**: `sandboxer_v0/commentary.py build_commentary()` —
  deterministic from replay frames, provenance="deterministic". Every action
  event gets play_by_play at its frame; analyst beat every ~3rd action, on
  repeated-offense streaks, and on lopsided defense-promotion timing;
  >15s dead air filled with recaps of observed events only. Fails closed
  (VIDEO_DENSE_COMMENTARY_INVALID) on ungrounded lines. 26-event fixture:
  37 lines vs v7's 11.
- **Emotion cues**: `sandboxer_v0/commentary_emotion.py annotate()` — Fish S2.x
  free-form bracket cues ([excited], [tense, urgent], [calm, analytical],
  [warm, conversational]) applied ONLY in the TTS spoken path; manifest caption
  text stays clean. Cued text is what gets hashed (TTS resume consistent).
  Provenance keys emotion_applied/emotion_version in broadcast record.
- **Arena layout**: `video/src/arena-layout.js` computeDefenseLayout() — pure
  CommonJS grid packer with logo exclusion boxes, adaptive node size/rows,
  hard overflow error instead of overlap; beats target nodes by id.
  Tested via pytest shelling out to real JS (pilot/tests/test_arena_layout.py).

## Launching a series

```bash
cd pilot && sudo .venv/bin/python -m sandboxer_v0.job_runner launch \
  --job-id episode-v8 --log logs/jobs/episode-v8.log -- \
  .venv/bin/python scripts/run_series.py \
  --image /var/lib/sandboxer/images/runner-alpine-3.24.1-r67.qcow2 \
  --profile /var/lib/sandboxer/images/runner-alpine-3.24.1-r67.profile.json \
  --series-id episode-v8
```

Bridge smoke-test first (~20s claimed in runbook but measured 166s warm-up on
this host — allow 300s timeout). Watchdog cron every minute, repeat ~120,
dedup instruction ("[SILENT] if unchanged"), self-reports final series result.

## User feedback encoded (v7 review, drives v8 requirements)

1. Episode MUST contain three matches (was one in v7).
2. Commentary must narrate EVERY action including the whole defensive phase,
   synced to facts, no invention.
3. Interviews between phases + model thoughts + commentator opinions wanted.
4. Defense-graph nodes must never overlap model logos.
5. Emotional intonation per line via Fish cues.
6. Narrate observed timing asymmetries (v7: Laguna took ~10x Muse to promote
   its blue defense — verified from telemetry request/deploy timestamps; it is
   model inference time, NOT a pipeline bug).

## Delegation notes (opencode)

- Model "ox-alpha" does NOT exist as an opencode model — passing it yields a
  generic server error. Available: x-preview-f-free (default in config),
  hy3-free, mimo-v2.5-free, muse-spark-1.2-contributor-free, nemotron-*.
  Omit --model to use configured default.
- Sequence used: task5 arena fix → task2 interviews → task3 dense commentary →
  task4 emotion TTS → task1 best-of-3 (biggest last). One opencode task per
  concern, each with tests, verify independently then commit yourself.
- Tasks take 10–30 min each; launch background with notify_on_complete and
  poll git status for progress (files appearing = working).
