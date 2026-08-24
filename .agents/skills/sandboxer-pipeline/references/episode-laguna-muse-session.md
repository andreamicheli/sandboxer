# Episode laguna-muse (2026-08-23/24): v8d → v3 full pipeline run

End-to-end iteration log: series fixes, TTS speak-window hardening, Remotion
inputProps bug, disk-full incident, and the final successful publish.

## Series results

| Series | Result | Cause |
|---|---|---|
| episode-v8d | 2/3 VALID | m3 CONTROL_BOOTSTRAP_TIMEOUT (infra) |
| episode-laguna-muse | 1/3 VALID | m2/m3 bare RuntimeError at 3 telemetry events, no retry existed |
| episode-laguna-muse-v2 | 1/3 VALID | provider slowness: thinking-loop timeouts at 180s |
| **episode-laguna-muse-v3** | **2/3 VALID_CAPTURE** ✅ | phase-timeout 300 gave slow providers room |

Winner alternation across series (Laguna, Laguna, Muse 2-0): good editorial
variety, no predictable outcome.

## Fixes committed (all with tests)

1. `run_series.py` `_is_retryable_failure`: generic RuntimeError/FileNotFoundError/
   OSError/TimeoutError now get exactly ONE retry. Previously only
   `*BUDGET_EXCEEDED` and `COMMAND_CODE_TIMEOUT` retried, so infra flake sank
   series below the gate unchallenged.
2. `commentary.py` recap clamp: lines need >=1s runway before the speak
   boundary (`start_frame + fps <= boundary`), applied on BOTH the series path
   and the legacy path in build_real_artifacts.py.
3. `commentary.py` speech budget calibrated to MEASURED Fish pace (~12.4 chars/s,
   not assumed 15) + LINE_TARGET_SECONDS 4.0 -> 3.5 + ANALYST_EVERY 3->6 +
   compact telemetry note + streak pattern note replaces per-action play-by-play.
4. `tts.py`: trailing-silence trim per block (~120ms natural tail kept, RIFF/data
   sizes rewritten so streaming WAVs stay valid); two-pass packer — pass 1 hard
   anchors, pass 2 (only if tail overflows) pulls late blocks up to 8s earlier
   with zero gap; fail-closed if TOTAL speech alone exceeds the window.
5. `artifact_converter.py`: matches that died at bootstrap emit NO start/teardown/
   interview narration; zero-frame replays allowed in series merge.

## Remotion inputProps wrapper bug (critical)

The component signature is `({ manifest }) => ...`. Passing the manifest JSON
DIRECTLY as inputProps means `props.manifest` is undefined inside
calculateMetadata, which fails silently and Remotion falls back to the static
1500-frame default composition. Symptom: rendered video 50s instead of 1280s.

Fix: always wrap — `const inputProps = { manifest }`. Add a guard:
```
if (composition.durationInFrames !== expectedFrames) throw new Error('DURATION_MISMATCH');
```
before renderMedia. Note: getCompositions/selectComposition swallow
calculateMetadata errors silently; verbose logging shows it ran but not that it
fell back.

## Disk-full incident

12 stale remotion-webpack-bundle-* dirs (~340MB each) filled the disk to 100%
(ENOSPC during bundle). Also 66 runner images ~184MB each in
/var/lib/sandboxer/images/. Cleanup: delete all but last-3 runner images,
delete all webpack bundles after each render. Keep 13GB+ free before rendering.

## Orphaned QEMU processes

When the match runner dies silently (rare, cause still unidentified), its two
QEMU children survive for hours. Check with `ps aux | grep -c "[q]emu-system"`;
kill by PID from the sandboxer-ttl watchdog journal entries. TTL systemd units
failing (`sandboxer-ttl-*: exit-code`) are a symptom of already-dead runners.

## Publish flow quirks

- `publish_broadcast.py --youtube real` requires bundle_path/report_url/
  publications_index unless `--allow-ungated` (still an open debt).
- BGM underlay needs `--voice-track <run>/commentary-full.wav` explicitly;
  the default resolution assumes a different out-dir layout.
- Use `--no-bgm` when re-publishing an already-mixed broadcast-bgm.mp4.

## Full command sequence that worked (v3)

```bash
# 1. series (sudo needed for KVM)
cd pilot && sudo env PATH="$PATH" .venv/bin/python scripts/run_series.py \
  --image /var/lib/sandboxer/images/<latest>.qcow2 \
  --profile /var/lib/sandboxer/images/<latest>.profile.json \
  --series-id <id> --phase-timeout 300

# 2. artifacts + TTS (build as root, chown back, TTS without sudo)
sudo .venv/bin/python scripts/build_real_artifacts.py --series ... --out-dir artifacts/runs/<id>
sudo chown -R ubuntu:ubuntu artifacts/runs/<id>/
set -a && source .env && set +a
.venv/bin/python scripts/render_commentary_audio.py --manifest ... --track ...

# 3. render via node script (see video/render-video.js pattern), verify duration
#    matches manifest frames/fps BEFORE muxing

# 4. mux + publish
ffmpeg -y -i video-only.mp4 -i commentary-full.wav -c:v copy -c:a aac -b:a 192k -shortest broadcast.mp4
.venv/bin/python scripts/publish_broadcast.py --manifest ... --video broadcast-bgm.mp4 \
  --youtube real --privacy unlisted --no-bgm --allow-ungated
```
