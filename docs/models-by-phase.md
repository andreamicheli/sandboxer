# Modelli per fase — mappa esatta

Come i modelli LLM sono usati in ogni fase della pipeline, con gli **ID esatti**.
Aggiorna questo file quando cambia un default in `sandboxer_v0/agents.py`,
`sandboxer_v0/review_pipeline.py`, `sandboxer_v0/tts.py` o `pilot/config.json`.

```mermaid
flowchart TD
    subgraph MATCH["① Match · il duello (Command Code `cmd`)"]
        direction LR
        C1["Competitor A<br/><b>poolside/laguna-s-2.1-free</b><br/>Laguna"]
        C2["Competitor B<br/><b>meta/muse-spark-1.2-contributor</b><br/>Muse Spark"]
        UI["Interview<br/>(stessi modelli, senza tool)"]
        C1 --> RED1[Red · attacca]
        C2 --> RED2[Red · attacca]
        RED1 --> WIN["Vincitore =<br/>verifica flag (deterministico)"]
        RED2 --> WIN
    end

    subgraph CONTENT["② Produzione contenuti (agents.py — env SANDBOXER_&lt;FASE&gt;_AGENT)"]
        direction LR
        P1["Commentary<br/><b>codex</b>"]
        P2["Intro / greeting<br/><b>codex</b>"]
        P3["Arena plan<br/><b>cmd</b>"]
        P4["Report narrative<br/><b>codex</b>"]
    end

    subgraph REVIEW["③ Review chain (review_pipeline.py)"]
        direction LR
        R1["gemini_evidence_triage<br/><b>agy</b>"]
        R2["luna_source_discovery<br/><b>codex</b>"]
        R3["sonnet_behavioral_analysis<br/><b>cmd</b>"]
        R4["terra_skeptical_review<br/><b>agy</b>"]
        R5["sol_final_synthesis<br/><b>codex</b>"]
        R1 --> R2 --> R3 --> R4 --> R5
    end

    subgraph AUDIO["④ Audio (tts.py)"]
        direction LR
        T1["Fish Audio<br/><b>s2.1-pro-free</b><br/>voci: Kore + Charon"]
        T2["Fallback Gemini<br/><b>gemini-3.1-flash-tts-preview</b>"]
    end

    subgraph DET["⑤ Deterministico · nessun LLM"]
        direction LR
        D1["Report canonico"]
        D2["Report LaTeX + grafici"]
        D3["Render Remotion"]
        D4["Mux ffmpeg"]
        D5["Upload YouTube"]
        D6["Scheduling + sito"]
    end

    MATCH --> CONTENT --> REVIEW --> AUDIO --> DET
```

## Tabella riassuntiva

| Fase | Modulo | Agente/Modello | ID esatto | Fallback deterministico |
|---|---|---|---|---|
| **Match — Blue/Red/Interview** | `command_code.py` | `cmd` (Command Code) | `poolside/laguna-s-2.1-free` · `meta/muse-spark-1.2-contributor` | — (esito sempre da verifica flag) |
| **Commentary** (telecronaca) | `commentary.py` | `codex` (OpenAI Codex) | **`gpt-5.6-luna`** (Luna 5.6) | `draft_commentary` fallisce chiuso |
| **Intro / greeting** | `commentary.py` | `agy` (Antigravity Gemini) | **`gemini-3.7-flash-high`** (Gemini 3.7) | rundown deterministico del caller |
| **Arena plan** (coreografia) | `arena_visual.py` | `cmd` (Command Code) | **`meta/muse-spark-1.2-contributor`** (Muse Spark Contributor) | `FakeArenaVisualDrafter` |
| **Report narrative** | `report_narrative.py` | `agy` (Antigravity Gemini) | **`gemini-3.7-flash-high`** (Gemini 3.7) | `DeterministicReportNarrativeDrafter` |
| **Review — evidence triage** | `review_pipeline.py` | `agy` (Antigravity Gemini) | `gemini-3.7-flash-high` | — |
| **Review — source discovery** | `review_pipeline.py` | `codex` | **`gpt-5.6-luna`** | — |
| **Review — behavioral analysis** | `review_pipeline.py` | `cmd` | **`meta/muse-spark-1.2-contributor`** | — |
| **Review — skeptical review** | `review_pipeline.py` | `agy` | `gemini-3.7-flash-high` | — |
| **Review — final synthesis** | `review_pipeline.py` | `codex` | **`gpt-5.6-luna`** | — |
| **TTS (voce)** | `tts.py` | Fish Audio | `s2.1-pro-free` · voci Kore/Charon (`reference_id`) | Gemini fallback |
| **Report canonico** | `report.py` | — nessun LLM | deterministico dal bundle | — |
| **Report LaTeX + grafici** | `report_latex.py` | — nessun LLM | deterministico (matplotlib) | — |
| **Render / mux / upload / sito** | `video.py`, `youtube.py`, `schedule.py` | — nessun LLM | deterministico | — |

## Note

- **Override per fase:** `SANDBOXER_<FASE>_AGENT=codex|cmd|agy` (es. `SANDBOXER_COMMENTARY_AGENT=cmd`) e `SANDBOXER_<FASE>_MODEL=<id>` per il modello. Default in `PHASE_DEFAULTS` (`agents.py`): commentary=`codex`, arena=`cmd`, report_narrative=`agy`, intro=`agy`.
- **Modelli default degli agenti** (`agents.py`): `codex` → **`gpt-5.6-luna`** (Luna 5.6), `cmd` → **`meta/muse-spark-1.2-contributor`** (Muse Spark Contributor), `agy` → **`gemini-3.7-flash-high`** (Gemini 3.7).
- **`agy` è geo-bloccato** su questo host (Antigravity `FAILED_PRECONDITION: User location is not supported`): le fasi che lo preferirebbero ricadono su `cmd`/`codex` o sul drafter deterministico. L'adapter è comunque implementato e funzionerà in una regione supportata.
- **Report**: i *fatti* (vincitore, esiti, budget, hash) sono sempre derivati meccanicamente dal bundle firmato; i modelli scrivono solo la *prosa* (narrative, commentary, intro), validata fail-closed e con fallback deterministico.
