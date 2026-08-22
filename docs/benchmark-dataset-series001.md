# SandBoxer Series 001 — Benchmark dataset preventivo (15 match)

Raccolto 2026-08-22. Fonti: Artificial Analysis, poolside.ai, aireleasetracker.com,
venturebeat.com, aimodelsnavi.com, static.stepfun.com, aicybr.com, orcarouter.ai,
morphllm.com, kingy.ai, wan27.org, datacamp.com, benchlm.ai.

Regole:
- Valori in frazione 0-1. Priorità: stesso benchmark + stessa versione per entrambi
  i modelli del pair. Se un modello non ha score pubblicato su quel benchmark,
  la riga NON entra nel pair (niente stime inventate).
- Ogni riga porta `source` e `harness_note` quando le versioni differiscono.
- I 6 modelli: Laguna S 2.1 (Poolside), Muse Spark 1.2 (Meta), Tencent Hy3,
  DeepSeek V4 Flash/Pro, Step 3.7 Flash, Qwen 3.7 Max. Il roster dei pair reali
  va confermato; sotto si assume round-robin con 15 match.

## Punteggi di riferimento per modello

| Benchmark | Laguna S 2.1 | Muse Spark 1.2 | Tencent Hy3 | DeepSeek V4 Flash 0731 | DeepSeek V4 Pro 0813 | Step 3.7 Flash | Qwen 3.7 Max |
|---|---|---|---|---|---|---|---|
| Terminal-Bench 2.1 | 0.702 | 0.829 | 0.644* | 0.827 | 0.879† | 0.595 | 0.697‡ |
| SWE-bench Pro | 0.594 | 0.550 | — | 0.526 | 0.554 | 0.563 | 0.606 |
| SWE-bench Verified/Multilingua | 0.785 (ML) | 0.866 (V, vals.ai) | — | 0.790 (V) | 0.806 (V) | — | 0.783 (ML) |
| DeepSWE 1.1 | 0.404 | 0.593 | — | 0.544 | 0.627 | — | — |
| Toolathlon (Verified) | 0.497 | 0.494 | — | 0.703§ | 0.741† | ~0.50¶ | — |
| MCP Atlas | 0.742 | n/p | — | 0.674 | 0.736 | — | 0.764 |
| GDPval-AA v2 / AA Index | 0.74 Elo-code rescaled | AA II 0.54 | AA II 0.42 | AA II 0.50 | AA II 0.53 | AA II 0.30 | AA II 0.566 |
| SciCode | — | 0.56 | 0.476 | — | — | — | 0.535 |

\* commandcode.ai (TB senza versione esplicita). † vendor-reported (DeepSeek harness),
indipendente ~0.787. ‡ Terminal-Bench 2.0-Terminus, non 2.1: confronto direzionale.
§ Toolathlon non-Verified salto versione. ¶ stepfun internal fixed version.

## Pair preventivi (round-robin, 15 match)

Per ogni pair elenco i benchmark dual-published utilizzabili subito.

1. **Laguna vs Muse Spark 1.2** — TB 2.1, SWE-Pro, DeepSWE 1.1, Toolathlon V, MCP Atlas
   (già implementato, 5 righe)
2. **Laguna vs Tencent Hy3** — TB (versioni diverse, nota obbligatoria), AA Coding
3. **Laguna vs DeepSeek V4 Flash** — TB 2.1, SWE-Pro, DeepSWE, MCP Atlas, Toolathlon*
4. **Laguna vs DeepSeek V4 Pro** — TB 2.1, SWE-Pro, DeepSWE, MCP Atlas, Toolathlon
5. **Laguna vs Step 3.7 Flash** — SWE-Pro, TB 2.1
6. **Laguna vs Qwen 3.7 Max** — SWE-Pro, TB (nota 2.0-Terminus), MCP Atlas
7. **Muse Spark 1.2 vs Tencent Hy3** — TB, SciCode, AA Index
8. **Muse Spark 1.2 vs DeepSeek V4 Flash** — TB 2.1, SWE-V (harness diversi), DeepSWE
9. **Muse Spark 1.2 vs DeepSeek V4 Pro** — TB 2.1, SWE-V/Pro (harness), DeepSWE
10. **Muse Spark 1.2 vs Step 3.7 Flash** — TB 2.1, SWE-Pro
11. **Muse Spark 1.2 vs Qwen 3.7 Max** — TB (2.0 vs 2.1, nota), MCP Atlas, SWE
12. **Tencent Hy3 vs DeepSeek V4 Flash** — TB, SciCode, AA Index
13. **Tencent Hy3 vs DeepSeek V4 Pro** — TB, SciCode, AA Index
14. **DeepSeek V4 Flash vs Step 3.7 Flash** — TB 2.1, SWE-Pro, AA Index
15. **Qwen 3.7 Max vs DeepSeek V4 Pro** — SWE-Pro, MCP Atlas, TB (note versione)

Pair con copertura minima (< 2 righe dual-published pulite): nessuno; il peggiore
è #2 e #12 che vivono di TB cross-version + AA Index. Se vuoi coperture più pulite,
i candidati sostitutivi sono Kimi K3, GLM-5.2, Grok 4.5, Claude Fable 5, GPT-5.6
(tutti con tabelle pubbliche ricche).

## Azione pipeline

`benchmark_snapshot` resta deterministico ma passa da hardcode a file dati:
`pilot/data/benchmark_snapshot.json`, una sezione per pair (chiave =
"model-a vs model-b"), consumata da build_real_artifacts.py. La scelta delle righe
per pair segue la regola "dual-published only", con harness_note mostrata a video
sotto il nome benchmark quando le versioni non coincidono.
