# Pipeline v7 — stato attuale (aggiornato al fallimento render)

## Match v7

| Campo | Valore |
|-------|--------|
| ID | match-full-pipeline-v7 |
| Stato | completato, exit_code 0 |
| Winner | poolside/laguna-s-2.1-free |
| Reason | SOLE_CAPTURE (Laguna ha catturato il flag di Muse, non viceversa) |
| Lunga | poolside/laguna-s-2.1-free |
| Perdente | meta/muse-spark-1.2-contributor |
| Job runner | sudo, runner image runner-alpine-3.24.1-r67.qcow2, profile runner-alpine-3.24.1-r67.profile.json |
| Bridge cmd | stealth/ox-alpha, verifica CMD_OK (~112s) |

## Artifact prodotti

| Artifacts | Stato | Path |
|-----------|--------|------|
| replay.json | ✅ | pilot/artifacts/runs/match-full-pipeline-v7/replay.json |
| report.json | ✅ | pilot/artifacts/runs/match-full-pipeline-v7/report.json |
| video_manifest.json | ✅ | pilot/artifacts/runs/match-full-pipeline-v7/video_manifest.json |
| Telemetry | ✅ | pilot/logs/evidence/match-full-pipeline-v7.telemetry.jsonl |
| Result | ✅ | pilot/logs/evidence/match-full-pipeline-v7.result.json |
| TTS commentary-full.wav | ✅ | pilot/artifacts/runs/match-full-pipeline-v7/broadcast.audio/commentary-full.wav (284.8s, 13.6 MB, hash 67fc318e6395, 11 blocchi, zero drift) |
| Remotion frames (26) | ✅ | pilot/artifacts/runs/match-full-pipeline-v7/replay/ |

## Video render

| Step | Stato | Note |
|------|--------|------|
| video-only.mp4 (Remotion) | ❌ FALLITO | Timeout/OOM sulla scena custom_intro con OffthreadVideo |
| Mux BGM + voice → delivery.mp4 | ⏳ In attesa | Richiede video-only.mp4 pronto |
| Publish unlisted YouTube | ⏳ In attesa | Richiede delivery.mp4 pronto |

## Cosa manca per completare la pipeline

1. **video-only.mp4**: il render Remotion fallisce con OOM/timeout sulla scena custom_intro (OffthreadVideo con video H.264). RAM disponibile ≤ 1 GB. Cause principali:
   - Chromium headless (~400-500 MB) + decodifica video H.264 + RAM totale 3 GB con ~1 GB libero
   - Processi che occupano RAM: t3 serve (~400 MB RSS), Hermes gateway (~450 MB), opencode x2 (~230 MB ciascuno), codex (~180 MB)
   - Il watchdog match-watch-v7 è in pausa (enabled=false) — match finito, non serve più monitorare

2. **delivery.mp4**: solo possibile dopo video-only.mp4

3. **Publish**: solo possibile dopo delivery.mp4

## Azioni di mitigazione già provate

- Copiato custom-intro.mp4 da public/assets/ a assets/ (risolve timeout frame 84 iniziale, ma non il OOM dopo)
- Transcodificato intro a 30fps (ffmpeg baseline yuv420p) — ffmpeg stessa sembra andare in OOM
- Kill di freebuff (PID 2110057), agy (PID 1626898), t3 serve (PID 271670 con kill -9), opencode secondario (PID 2688049) — RAM salita a ~2 GB disponibili
- Rilanciato render con più RAM: ha ancora fallito con `registerRoot is not defined` (problema API Remotion 4.x, non RAM)
- Fix: aggiunto `registerRoot(Composition)` all'entry point — render riprovato ma non completato
- Provata anche via CLI `remotion render` con timeout 60s per frame — stesso risultato OOM/timeout

## Fallback considerati

- Scena custom_intro senza video file (SVG/HTML statico + fade) — code già scritto in un tentativo precedente ma non usato
- Rendering con `@remotion/renderer` (renderMedia / renderFrames) invece del CLI
- Rimuovere completamente la scena video dalla composizione

## Post다음에

Se il video non viene generato, ci sono due strade:
1. Documentare il fallimento e fermarsi (il match è comunque riuscito, gli artifact sono prodotti)
2. Proseguire con fallback (scena senza video, o render alternativo)
