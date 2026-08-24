# Commentary vs TTS speak window (episode-v8d/v8g postmortem chain)

The dense deterministic commentator generates lines anchored to event frames;
the TTS packer must fit all rendered speech before the closing recap scene.
This chain of fixes is now in the repo, but the *invariants* are worth knowing
before touching either side.

## Invariants

1. **Speak boundary** = sum of all scene durations except the last
   (`factual_recap`). No speech may start or end after it.
2. **Runway rule**: a line needs `start_frame + fps <= boundary`. A teardown
   line anchored 1 frame before the boundary has no room for its audio —
   filter it out at build time (`commentary.py` speak_boundary clamp AND the
   mirror clamp in `build_real_artifacts.py`; both paths exist).
3. **Planned starts are lower bounds**, not schedule targets. The packer
   places each block at `max(planned_start, prev_end + gap)`.
4. **Measured Fish pace is ~12.4 chars/s**, not the 15 originally assumed.
   `_CHARS_PER_SECOND = 12.4` drives the per-line text tightening
   (LINE_TARGET_SECONDS = 3.5).
5. **Fish blocks carry ~100-200ms trailing silence each.**
   `_trim_trailing_silence` (tts.py) strips it keeping a 120ms natural tail;
   across ~60 blocks that recovered ~8s. It handles streaming WAVs (placeholder
   RIFF sizes) via `_wav_data_bounds` — do NOT use `wave.open` on raw adapter
   output, it throws on placeholder headers.

## Packer strategy (tts.py render_commentary_audio)

- Pass 1: planned anchors hard-pinned, gap shrinks from 400ms toward floor
  25ms proportional to slack.
- Pass 2 (only if pass-1 tail overflows): blocks in the final pull zone
  (last 30s, never before 75% of window elapsed) may start up to 8s earlier
  than their anchor; gap floor drops to 0.
- Last resort: drop latest blocks one by one and re-pack.
- Fail closed (`COMMENTARY_OVERFLOW_AFTER_RENDER`) when TOTAL speech alone
  exceeds the window — no packing can fix that.

## Content-side levers (commentary.py), cheapest first

- `ANALYST_EVERY` cadence recaps: 3 → 4 → 6 as density grows. Each averaged
  ~12s of speech before shortening.
- Slow-promotion telemetry note compacted from a 10.6s sentence to ~4s
  ("Telemetry note: X promoted in Ns; Y in Ms.").
- Repeated-offense streak: when the pattern note fires (REPEATED_STREAK=3),
  suppress the per-action play-by-play lines it covers via
  `_suppressed_event_ids` — three identical "probes" lines anchored within
  2s are pure cascade fuel.
- Matches that die before gameplay (no winner): converter emits NO
  start/teardown/interview narration; series builder tolerates zero-frame
  replays (skip clock-base math); their scene renders as a placeholder.

## Debug recipe

Simulate packing offline before re-rendering TTS (blocks are cached by
script hash, so a manifest-only change still needs fresh WAVs if texts
changed):

```python
from sandboxer_v0.tts import _duration_ms
durs=[_duration_ms(open(f,'rb').read()) for f in sorted(glob('...block-*.wav'))]
# replay pass logic over manifest['commentary'] zipped with durs
# compare total speech + gaps against recap_start*1000//fps
```

Check trailing silence per block before blaming pacing: sum
`tail_frames/rate` walking back under threshold 300.
