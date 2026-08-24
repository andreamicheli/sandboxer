# TTS speak-window hardening (episode-v8d → laguna-muse-v3, 2026-08-23/24)

Session-verified fixes for the `COMMENTARY_OVERFLOW_AFTER_RENDER` failure class.
All committed to the repo; this file records WHY each knob exists so future
sessions don't re-tune them blindly.

## The failure class

Dense series commentary (56–64 lines) overflows the speak window (scene total
minus recap). The TTS guardrail raises `COMMENTARY_OVERFLOW_AFTER_RENDER`
after rendering, wasting a full Fish TTS pass (~10–15 min). Multiple root
causes stack; fixing only one leaves the error in place.

## Fix layers (all needed; check in this order)

1. **No narration for matches without gameplay** (`artifact_converter.py`):
   a match that died at bootstrap (no winner) emits no match_started /
   phase / interview / teardown lines. Its scene renders as placeholder.
   - Pitfall: `str(result.get("winner"))` is truthy for JSON null
     (`str(None)=='None'`). Test the value directly.

2. **Recap clamp needs runway** (`build_real_artifacts.py` + `commentary.py`):
   lines anchored within 1 frame of the recap boundary leave no room for their
   audio. Filter: `start_frame + fps <= boundary`, and clamp
   `end_frame = min(end_frame, boundary - 1)` but never below `start + fps`.
   Apply on BOTH series and legacy paths — they have separate clamp sites;
   patching one silently leaves the other broken (test caught it).

3. **Zero-frame replays are legal** (`build_real_artifacts.py`): after fix 1 a
   bootstrap-failed match converts to an empty replay. Series merge must skip
   clock-base math for it (`if not replay.get("frames"): continue`) instead of
   raising `SERIES_REPLAY_EMPTY`.

4. **Calibrated speech estimate** (`commentary.py`): Fish s2.1 speaks ~12.4
   chars/s measured (not the assumed 15). `LINE_TARGET_SECONDS=3.5`,
   `ANALYST_EVERY=6` (cadence analyst recaps averaged 12s each).

5. **Compact telemetry note**: the slow-promotion note was 10.6s spoken;
   same facts fit ~4s ("Telemetry note: X promoted in 41s; Y in 18s.").

6. **Streak suppression** (`commentary.py`): when REPEATED_STREAK fires, drop
   the covered actions' play-by-play lines (pattern note narrates them all).
   Track suppressed event_ids in a set built during the queue pass, filter in
   the emit pass.

7. **Trailing-silence trim** (`tts.py::_trim_trailing_silence`): Fish blocks
   carry ~100–200ms silence each; ~8s across a dense series. Trim to a 120ms
   natural tail, rewrite RIFF+data sizes. MUST use `_wav_data_bounds` not
   `wave.open` — Fish streams WAVs with placeholder 0xFFFFFFFF header sizes.

8. **Two-pass packer** (`tts.py`): pass 1 keeps event anchors as hard lower
   bounds; if tail still overflows, pass 2 allows pulling late-zone blocks up
   to `_PULL_FORWARD_MS=8000` earlier (zone = last 30s AND final quarter of
   window), gap floor 0. As last resort drop latest block and re-pack
   (drop-to-fit). Fail closed ONLY when total speech alone exceeds the window
   (nothing can fit it).

## Debugging recipe (measured, don't guess)

```python
import json, glob, sys
sys.path.insert(0,'.')
from sandboxer_v0.tts import _duration_ms
m=json.load(open('<run>/video_manifest.json'))
fps=m['fps']
recap_start=sum(s['duration_frames'] for s in m['scenes'][:-1])
files=sorted(glob.glob('<run>/broadcast.audio/block-*.wav'))
durs=[_duration_ms(open(f,'rb').read()) for f in files]
c=m['commentary']; prev=None
for i,(line,d) in enumerate(zip(c,durs)):
    planned=line['start_frame']*1000//fps
    at=planned if prev is None else max(planned, prev+25)
    if at+d > recap_start*1000//fps:
        print(f'crossing block {i}: planned {planned/1000:.1f} dur {d/1000:.1f}s | {line["text"][:50]}')
        break
    prev=at+d
print(f'speech {sum(durs)/1000:.1f}s vs window {recap_start/fps:.1f}s')
```

The first crossing block tells you which layer to reach for: planned start ≥
boundary → clamp/runway bug; speech total > window → density (layers 4–7);
single huge block → compact that text template.

## Test-suite gotchas

- 2 pre-existing env-dependent failures in `tests/test_tts.py` (Gemini/Fish
  adapters hitting real APIs from a blocked region). Verify against clean HEAD
  (`git stash && pytest && git stash pop`) before attributing them to your change.
- pytest run under some shells hangs on collection; run backgrounded with
  output to a file and poll, or add `-p no:cacheprovider`.
