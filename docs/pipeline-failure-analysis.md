# Problemi dell'ultima run: match Laguna vs Muse e generazione del video

## Scopo del documento

Questo documento sostituisce l'analisi generale precedente e riguarda **solo l'ultima run end-to-end** eseguita con:

- Laguna S 2.1 contro Muse Spark 1.2;
- Command Code per le chiamate dei competitor nell'arena;
- Hermes CLI con `orcarouter/free` come sostituto temporaneo di Codex per le fasi editoriali;
- Fish Audio `s2.1-pro-free` per il TTS;
- Remotion per il video;
- YouTube per l'upload unlisted;
- sito statico deployato su Vercel.

Per ogni problema sono indicati sintomo, causa, impatto e stato. Sono distinti i problemi che hanno bloccato una prima prova, quelli aggirati durante la stessa run e quelli ancora presenti alla fine.

## Risultato finale osservato

La run ha prodotto:

- un match finale con esito `VALID_CAPTURE`;
- Laguna S 2.1 vincitrice con `SOLE_CAPTURE`;
- una cattura di Laguna e nessuna cattura di Muse;
- un video Remotion muxato di circa 157 secondi e circa 12,5 MB;
- 16 blocchi audio Fish Audio;
- un upload YouTube unlisted con video ID `VVuiitjVQVU`;
- un broadcast record con `privacy_status: unlisted`.

La run **non ha completato correttamente la release editoriale**:

- il report specifico della run non è stato generato e pubblicato sul sito;
- `publications.json` è rimasto vuoto;
- `indexed` è rimasto `false`;
- `slug` è rimasto `null`;
- il report URL registrato nel broadcast record era il repository GitHub, non una pagina report specifica;
- alcuni manifest conservavano ancora `https://sandboxer.example/...`, un URL placeholder.

Quindi il giudizio corretto è:

> La parte competitiva e audiovisiva è arrivata a un risultato funzionante. La catena evidence → report → sito → descrizione YouTube non è arrivata a una release completa.

---

## 1. Sostituzione temporanea di Codex con Hermes

### Sintomo

Codex risultava autenticato, ma non era utilizzabile in modo affidabile come agente headless dalla pipeline. La directory trusted e il login ChatGPT non garantivano una chiamata automatica riproducibile.

Le fasi che dipendevano da Codex rischiavano quindi di bloccarsi o di ricadere in un fallback deterministico.

### Causa

La pipeline assumeva che un login interattivo a un coding agent equivalesse a un endpoint unattended. Nell'ambiente corrente questa equivalenza non valeva.

### Impatto

Non era possibile usare Codex come produttore automatico di:

- commentary;
- intro;
- contenuti narrativi;
- alcuni piani visuali.

### Mitigazione applicata

È stato aggiunto un `HermesAdapter` che invoca Hermes CLI con il modello `orcarouter/free`. La sostituzione è stata fatta solo per questa run e non deve essere interpretata come rimozione definitiva di Codex.

È stata verificata anche una chiamata reale con tool di lettura, scrittura e terminale prima di usarla nella pipeline editoriale.

### Stato

**Aggirato, non consolidato.** La provenienza dell'agente deve diventare parte obbligatoria del manifest di ogni fase.

---

## 2. Output LLM vuoto, lento o non conforme

### Sintomo

Le prime generazioni con Hermes hanno prodotto, a seconda del tentativo:

- `COMMENTARY_EMPTY`;
- JSON malformato;
- output con markdown fence o testo prima/dopo il JSON;
- timeout;
- risposte valide in isolamento ma fallimenti quando commentary, intro e arena venivano richiesti consecutivamente.

### Causa

`orcarouter/free` è un endpoint condiviso con latenza e disponibilità variabili. I prompt più pesanti, soprattutto l'arena plan, aumentavano la probabilità di timeout o risposta incompleta.

Inoltre i parser assumevano che il modello restituisse JSON puro, senza fence Markdown né testo esplicativo.

### Impatto

La generazione non era deterministica dal punto di vista operativo: la stessa pipeline poteva riuscire con una chiamata isolata e fallire con tre chiamate sequenziali.

### Mitigazioni applicate

- retry nell'adapter Hermes;
- parser più tollerante a fence e testo circostante;
- test reale dell'adapter;
- chiamata Hermes mantenuta per commentary e intro;
- arena plan prodotto in modo deterministico per questa run, evitando una terza chiamata LLM costosa e fragile.

### Stato

**Parzialmente risolto.** Il free tier rimane un punto di fragilità e non c'è ancora un job queue con retry persistente, backoff e cache per fase.

---

## 3. Primo commentary generato troppo lungo

### Sintomo

Una bozza ha prodotto circa 20 linee. Le linee eccedevano lo spazio temporale disponibile e potevano invadere scene successive.

### Causa

Il prompt non imponeva un contratto abbastanza rigido per:

- numero massimo di linee;
- numero massimo di parole;
- durata massima;
- numero di interventi per voce;
- scena di appartenenza.

### Impatto

Il testo poteva essere valido semanticamente ma impossibile da sincronizzare con audio, match e recap.

### Correzione applicata

Il prompt è stato ristretto e il validatore è stato aggiornato per imporre un limite al numero di linee. La bozza finale usata nel video contiene 16 blocchi.

### Stato

**Risolto nella run**, ma il contratto dovrebbe essere espresso nello schema, non solo nel prompt.

---

## 4. Intro con offset fuori dalla scena

### Sintomo

La prima intro LLM conteneva linee ridondanti e offset fuori dal cold open. Alcune battute potevano iniziare durante il match, creando bleed o sovrapposizione con il commentary della fase competitiva.

### Causa

Gli offset generati dal modello erano trattati come autorevoli. Lo scheduler non limitava sempre le righe alla finestra pre-match e non riposizionava tutte le battute in sequenza.

### Impatto

Il saluto e la presentazione dei modelli non erano garantiti prima dell'inizio del combattimento.

### Correzione applicata

Sono stati aggiunti:

- clamp degli offset alla finestra intro;
- ordinamento temporale;
- ripacking back-to-back;
- limite esplicito alle scene precedenti al match;
- riparazione delle righe non conformi alla tassonomia editoriale.

### Stato

**Risolto nella run.** Il principio da conservare è che il modello propone testo e intenzione, ma lo scheduler deterministico mantiene autorità sui tempi.

---

## 5. Rischio di fallback verbatim nel commentary

### Sintomo

Durante i primi tentativi il testo risultava didascalico e vicino alla lettura dei terminali.

### Causa

Quando il draft LLM falliva, il fallback deterministico costruiva battute direttamente dagli eventi tecnici. Questo garantiva un output, ma non una telecronaca naturale.

### Impatto

Il video rischiava di descrivere meccanicamente ogni tool call invece di offrire:

- conversazione tra le due voci;
- ritmo umano;
- curiosità;
- interpretazioni prudenti;
- continuità narrativa.

### Mitigazione applicata

È stata generata una nuova bozza con Hermes, con linee editoriali e osservate separate. Il rendering finale ha usato la bozza aggiornata, non il fallback verbatim iniziale.

### Stato

**Migliorato, ma il fallback resta pericoloso.** Il manifest deve dichiarare sempre se ogni blocco è `llm`, `deterministic_fallback` o `human`. Una release pubblica dovrebbe rifiutare il fallback non approvato.

---

## 6. Primo tentativo di match fallito con `COMMAND_CODE_TOOL_BOUNDARY_FAILURE`

### Sintomo

Il primo tentativo completo del match Laguna vs Muse si è chiuso con:

```text
COMMAND_CODE_TOOL_BOUNDARY_FAILURE
```

La telemetria mostrava una serie di `api_retry`, in particolare dopo le azioni della fase Red e dopo la sottomissione dell'obiettivo.

### Causa osservata

Il pattern era compatibile con un errore transitorio o rate limiting del provider Command Code, soprattutto sul free tier di Laguna. Un probe diretto successivo verso Laguna ha risposto correttamente.

Non è stata osservata una violazione del boundary come causa primaria di quel tentativo.

### Impatto

Il match non poteva essere conteggiato come risultato valido e il suo Match ID non poteva essere riutilizzato per un retry.

### Correzione applicata

- probe diretto del provider;
- nuovo Match ID;
- nuovo avvio del match;
- verifica finale dell'outcome e della telemetria.

### Stato

**Risolto per la run, non alla radice.** L'errore provider deve essere distinto in modo strutturato da una vera violazione MCP o boundary.

---

## 7. Processo del match interrotto dalla sessione e risorse orfane

### Sintomo

Un primo lancio sembrava non produrre log. In seguito è emerso che il match era partito, ma l'orchestratore era stato terminato quando la sessione del tool si era chiusa.

Sono rimasti temporaneamente:

- processi QEMU;
- directory runner;
- file di telemetria parziale;
- stato ambiguo del job.

### Causa

Il comando lungo non era stato inizialmente avviato come job supervisionato. Mancavano un launcher persistente, un PID file e un watchdog indipendente dal processo Python.

### Impatto

- consumo inutile di RAM e CPU;
- rischio di collisione con il retry;
- teardown non verificabile;
- confusione tra match ancora in esecuzione e match falliti.

### Correzione applicata

Le risorse residue sono state pulite manualmente. Il retry è stato avviato con `setsid`, `nohup` e log persistente.

### Stato

**Aggirato.** La soluzione corretta è un job runner persistente che garantisca teardown in caso di disconnessione.

---

## 8. Match ID duplicato durante il retry

### Sintomo

Un secondo avvio con lo stesso Match ID è stato rifiutato.

### Causa

Il primo processo era effettivamente partito, anche se il log non era stato subito visibile. Il sistema ha correttamente trattato il Match ID come già utilizzato.

### Impatto

È stato necessario scegliere un nuovo ID e ricostruire la supervisione del processo.

### Correzione applicata

È stato generato un nuovo Match ID per il tentativo valido.

### Stato

**Risolto nella run.** Il launcher dovrebbe verificare lo stato prima dell'avvio e generare automaticamente ID nuovi per ogni retry.

---

## 9. Telemetria reale non direttamente compatibile con replay e video

### Sintomo

Il match reale ha prodotto telemetria runtime e `result.json`, ma non un evidence bundle già pronto per il replay e il manifest video.

### Causa

I livelli avevano schemi differenti:

```text
telemetria runtime
result.json
frozen evidence bundle
replay.json
video-manifest.json
```

Non esisteva ancora un convertitore canonico completamente integrato.

### Impatto

È stato necessario creare `build_real_artifacts.py` per estrarre manualmente:

- eventi e frame;
- fasi;
- competitor;
- tool call redatte;
- difese;
- attacchi;
- outcome;
- dati per commentary e arena.

### Problema aggiuntivo

Durante i primi tentativi lo script di conversione aveva un bug nel percorso di fallback: usava `frames` prima che fosse definito. Il bug è stato corretto prima della generazione finale.

### Stato

**Funzionante per questa run, ma temporaneo.** Il convertitore deve diventare una fase ufficiale con schema, hash e validazione.

---

## 10. Arena visuale non LLM nella run finale

### Sintomo

Il piano dell'animazione dei competitor, delle difese e degli attacchi non è stato ottenuto da una chiamata LLM finale.

### Causa

Il prompt per l'arena era una delle chiamate più pesanti e instabili su `orcarouter/free`. La parte visuale aveva già una trasformazione deterministica dai dati del match, quindi è stata preferita per mantenere un output valido e riproducibile.

### Impatto

L'animazione è coerente con gli eventi estratti, ma non rappresenta una decisione LLM autonoma di layout o regia.

### Stato

**Scelta deliberata per questa run.** Il renderer Remotion e la geometria sono deterministici; l'eventuale uso LLM deve restare una proposta validata, mai una fonte non verificata di fatti.

---

## 11. TTS: primo avvio non osservabile, seconda esecuzione riuscita

### Sintomo

Il primo avvio del renderer TTS non ha lasciato un log osservabile e il controllo del processo non ha trovato un job attivo. È stato necessario rilanciarlo con un avvio detached esplicito.

### Causa

La gestione del processo lungo e del redirect dei log non era uniforme tra i comandi. La sessione poteva terminare senza lasciare uno stato persistente chiaro.

### Impatto

Non era possibile sapere subito se il TTS fosse fallito, terminato o semplicemente ancora in coda.

### Correzione applicata

Il job è stato rilanciato con `setsid` e log persistente. La seconda esecuzione ha prodotto 16 blocchi Fish Audio.

### Stato

**Risolto operativamente, non strutturalmente.** TTS, render e upload dovrebbero condividere lo stesso sistema di job e stato.

---

## 12. Contratto testo/istruzioni del TTS

### Sintomo

Nelle versioni precedenti del progetto le voci pronunciavano istruzioni come `pace`, `voice_role` o `voice_roll` invece della frase editoriale.

### Causa

Metadati di stile e testo parlato erano stati mescolati nel payload TTS.

### Impatto

La voce leggeva il controllo interno della sintesi, producendo un risultato innaturale e non adatto alla pubblicazione.

### Correzione applicata

Il contratto è stato separato:

- `script`: solo testo da leggere;
- `voice`: identificativo della voce;
- `style`: metadato tecnico e hash, non testo pronunciato.

Nella run finale il provider effettivo è stato Fish Audio `s2.1-pro-free`, con Kore per play-by-play e Charon per analyst.

### Stato

**Risolto per Fish Audio.** Ogni nuovo adapter deve avere test che dimostrino che il testo inviato non contiene metadati di regia.

---

## 13. Prima fase di rendering Remotion terminata per OOM

### Sintomo

Il rendering 1080p è stato terminato prima del completamento. La macchina aveva circa 3,7 GB di RAM.

### Causa

La concurrency predefinita di Remotion, insieme al rendering software e ai profili Chromium, richiedeva più memoria disponibile.

### Impatto

Il primo video non è stato prodotto.

### Correzione applicata

Il rendering è stato rilanciato con:

```text
--concurrency=1
```

### Stato

**Risolto nella run.** Il preflight dovrebbe calcolare automaticamente concurrency e qualità in base alla RAM.

---

## 14. `/tmp` tmpfs pieno durante il rendering

### Sintomo

Dopo i tentativi falliti, Remotion e Chromium non riuscivano più a usare in modo affidabile lo spazio temporaneo.

### Causa

`/tmp` era un tmpfs di circa 1,9 GB. Le directory residue `react-motion-render*` occupavano circa 734 MB.

### Impatto

Il render poteva fallire anche dopo la riduzione della concurrency, indipendentemente dalla correttezza del codice video.

### Correzione applicata

- rimozione delle directory temporanee stale;
- uso di una `TMPDIR` su disco persistente con più spazio;
- concurrency 1.

### Stato

**Risolto nella run.** Il renderer deve verificare spazio e TMPDIR prima di partire e pulire sempre i profili di job conclusi.

---

## 15. Recap hardcoded non coerente con il match

### Sintomo

Una versione iniziale del recap descriveva un risultato con entrambe le catture o budget exhausted, mentre il match reale era una cattura singola di Laguna.

### Causa

Il testo era hardcoded nel componente Remotion e non derivava dall'outcome del manifest.

### Impatto

Il video avrebbe potuto contraddire la telemetria e il report.

### Correzione applicata

È stato aggiunto `outcome_basis` al manifest e il recap è stato aggiornato per usare:

- `VALID_CAPTURE`;
- `SOLE_CAPTURE`;
- Laguna come vincitrice;
- difesa DENY/PUBLIC di Muse;
- difesa HEADER/HEADER di Laguna.

### Stato

**Risolto prima del render finale.** Nessun testo fattuale dovrebbe restare hardcoded nel componente video.

---

## 16. Drift tra manifest TTS e provider usato

### Sintomo

Il manifest conservava un contratto TTS Gemini, mentre il broadcast record registrava Fish Audio:

```json
{
  "model": "gemini-3.1-flash-tts-preview",
  "models_used": ["s2.1-pro-free"]
}
```

### Causa

La struttura del manifest era stata preparata quando Gemini era il provider previsto; la run ha poi usato Fish come provider principale senza riscrivere completamente tutti i campi derivati.

### Impatto

La provenance del video era ambigua e un revisore non poteva capire subito quale provider avesse prodotto l'audio.

### Stato

**Non completamente risolto nella run.** Il manifest deve distinguere `requested` e `observed`, oppure essere riscritto dopo il TTS con il provider effettivo.

---

## 17. Report non generato prima dell'upload

### Sintomo

Il video YouTube è stato caricato, ma il report specifico della run non era disponibile sul sito.

Il record mostrava:

```json
{
  "indexed": false,
  "slug": null,
  "report_url": "https://github.com/andreamicheli/sandboxer"
}
```

### Causa

La pubblicazione è stata eseguita senza un evidence bundle nel path atteso e senza completare il percorso:

```text
freeze bundle
-> build report
-> stage site
-> deploy Vercel
-> verify URL
-> upload YouTube
```

Senza `--bundle`, la funzione di staging del report restava un no-op e il publisher poteva usare il comportamento legacy basato su `--report-url`.

### Impatto

- nessuna pagina report della run;
- nessun PDF LaTeX della run caricato;
- `publications.json` vuoto;
- nessun banner o articolo indicizzato;
- descrizione YouTube priva del link al report specifico.

### Stato

**Aperto e prioritario.** È il problema più importante della release finale.

### Correzione necessaria

Il publisher deve bloccare l'upload reale se:

- il bundle non esiste;
- il report non è stato generato;
- il report URL è placeholder o generico;
- il deploy Vercel non è verificabile;
- `publications.json` non è stato aggiornato.

---

## 18. URL placeholder e URL GitHub usato come ripiego

### Sintomo

Gli artefatti contenevano:

```text
https://sandboxer.example/reports/e2e-hermes
```

mentre il broadcast record usava:

```text
https://github.com/andreamicheli/sandboxer
```

Nessuno dei due era il report pubblico specifico della run.

### Causa

Mancava una base URL reale e il sito non aveva ricevuto il report prima dell'upload.

### Impatto

Il pubblico non poteva aprire una pagina con analisi, evidenze, grafici e PDF relativi a Laguna vs Muse.

### Correzione necessaria

L'URL ufficiale del sito Vercel verificato è:

```text
https://sandboxer.vercel.app
```

Per una futura run il publisher deve costruire un URL specifico, per esempio:

```text
https://sandboxer.vercel.app/reports/<run-slug>
```

ma solo dopo che quella pagina esiste davvero e risponde HTTP 200.

### Stato

**URL di progetto aggiornato nella documentazione; report della run precedente ancora da rigenerare.** Non bisogna riscrivere retroattivamente l'artefatto come se il report fosse stato pubblicato.

---

## 19. Artefatti non isolati per run

### Sintomo

La directory `artifacts/` conteneva file con nomi stabili e provenienze diverse, tra cui video, audio, thumbnail, manifest e report.

### Causa

Le run riutilizzavano lo stesso workspace invece di usare una directory immutabile per `run_id`.

### Impatto

Esisteva il rischio di combinare:

- video di una run;
- audio di un'altra;
- manifest di una terza;
- report placeholder.

### Mitigazione applicata

Gli artefatti della run sono stati controllati manualmente e il video finale è stato verificato con ffprobe.

### Stato

**Non consolidato.** La struttura corretta è:

```text
artifacts/runs/<run_id>/
  evidence.json
  replay.json
  report.json
  video-manifest.json
  commentary-full.wav
  video-only.mp4
  delivery.mp4
  broadcast.json
```

Ogni file deve riportare `run_id`, hash dell'input e provenance del provider.

---

## 20. Tabella riassuntiva

| Fase | Sintomo | Causa | Esito nella run |
|---|---|---|---|
| Agenti editoriali | Codex non eseguibile headless | Login interattivo non equivalente ad API unattended | Hermes usato temporaneamente |
| Hermes | Output vuoto, JSON non conforme, timeout | Latenza/rate limiting e parser rigido | Retry e parser aggiornati |
| Arena | Terza chiamata LLM instabile | Prompt pesante | Piano deterministico |
| Commentary | Circa 20 linee, overflow | Budget editoriale assente | Prompt e validazione corretti |
| Intro | Offset nel match | Scheduler non vincolato | Clamp e ripack |
| Match 1 | `COMMAND_CODE_TOOL_BOUNDARY_FAILURE` | Probabile rate limiting provider | Nuovo Match ID, run valida |
| Orchestrazione | QEMU orfani e log mancanti | Processo non supervisionato | Cleanup manuale, `setsid`/`nohup` |
| Conversione | Telemetria non consumabile dal replay | Schemi non unificati | `build_real_artifacts.py` temporaneo |
| TTS | Primo job non osservabile | Gestione processi non persistente | Secondo job riuscito |
| Remotion | OOM | RAM ridotta e concurrency alta | `--concurrency=1` |
| Remotion | `/tmp` pieno | tmpfs piccolo e directory stale | Cleanup e TMPDIR su disco |
| Video | Recap non fattuale | Testo hardcoded | Derivazione da outcome_basis |
| Provenance | Gemini dichiarato, Fish usato | Manifest non riscritto | Drift ancora da sistemare |
| Report | Nessun report specifico online | Bundle e staging mancanti | Upload video riuscito, release incompleta |
| YouTube | Link al repo invece del report | Publisher non impone report/deploy | Gate P0 necessario |

---

## Priorità immediate

### P0: impedire una pubblicazione incompleta

1. Congelare il bundle reale prima di generare qualsiasi contenuto.
2. Generare report narrativo, JSON, grafici e PDF LaTeX nella directory della run.
3. Copiare gli asset nel sito e aggiornare `publications.json`.
4. Deployare il sito su `https://sandboxer.vercel.app`.
5. Verificare HTTP 200 del report specifico.
6. Solo dopo caricare il video su YouTube.
7. Inserire nella descrizione il report URL specifico, non il repository.
8. Bloccare l'upload quando uno dei passaggi precedenti manca.

### P1: rendere ripetibile il job

1. Un launcher persistente per match, TTS, render e upload.
2. `run-state.json` con stato, hash e provenance per fase.
3. Directory immutabile `artifacts/runs/<run_id>`.
4. Teardown indipendente dal parent process.
5. Retry con backoff e classificazione degli errori provider.
6. Validazione JSON e schema per ogni output LLM.
7. Provenance obbligatoria per ogni contenuto: agente, modello, provider e fallback.
8. Preflight RAM, spazio TMPDIR e concurrency.

### P2: migliorare la qualità editoriale

1. Budget temporale espresso nel contratto del draft.
2. Packing sempre basato sulla durata audio misurata.
3. Test automatico che impedisca al TTS di pronunciare metadati.
4. Verifica speech-to-text opzionale sui blocchi audio.
5. Separazione netta tra osservazione telemetrica e commento interpretativo.

---

## Pipeline corretta per la prossima esecuzione

```text
preflight
  -> match con job ID e supervisor
  -> teardown verificato
  -> freeze evidence bundle
  -> validazione bundle
  -> replay e manifest
  -> draft LLM con provenance
  -> validazione grounding e budget
  -> TTS e misura durata reale
  -> render Remotion con preflight risorse
  -> mux e ffprobe
  -> report JSON/HTML/LaTeX
  -> stage sito
  -> deploy Vercel
  -> verifica report HTTP 200
  -> upload YouTube unlisted
  -> verifica descrizione, captions e thumbnail
  -> broadcast record immutabile
```

La regola da introdurre nel publisher è semplice:

> Nessun upload reale se il report specifico della run non è già online e verificabile.

## Conclusione

L'ultima run ha dimostrato che il nucleo tecnico può arrivare a un video reale: il match finale è valido, l'audio Fish è stato prodotto, Remotion ha completato il render dopo l'adattamento alle risorse e YouTube ha accettato l'upload unlisted.

I problemi più importanti non sono stati nella logica dell'esito finale, ma nelle giunzioni tra le fasi:

- provider LLM instabili o non headless;
- output LLM non vincolato;
- job lunghi non supervisionati;
- telemetria non ancora trasformata da un passaggio canonico;
- risorse locali insufficienti per il render predefinito;
- provenance TTS non aggiornata;
- report non generato prima dell'upload.

La prossima release affidabile non richiede prima nuove animazioni. Richiede il consolidamento della catena:

```text
match valido
-> bundle congelato
-> report reale
-> sito aggiornato
-> URL verificato
-> video pubblicato
```
