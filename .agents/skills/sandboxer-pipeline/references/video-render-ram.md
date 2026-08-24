# Render Remotion su host con RAM limitata (≤ 2 GB disponibili)

## Problema

Il render Remotion con scena che usa `OffthreadVideo` (video H.264 da decodificare) va in OOM / timeout quando la RAM disponibile scende sotto ~1.5 GB. Chromium headless per il rendering consuma ~400-500 MB di base; la decodifica H.264 del video di intro (anche piccolo, es. 8s, 1280×720) aggiunge pressione, e con 1 GB disponibili il processo va in thrash e viene killed da OOM o va in timeout.

Sintomi:
- `TimeoutError: waiting for the page to render the React component at frame N failed: timeout 33000ms exceeded` (o simile per frame più tardi)
- `Compositor quit with signal SIGTERM`
- `delayRender() "Fetching ... from server" was called but not cleared after 58000ms`
- Il processo di render muore senza file di output

## Diagnosi

1. Controllare RAM disponibile prima del render:
   ```bash
   free -g
   ```
   Se `available` ≤ 1.5 GB, il render Remotion con video è a rischio.

2. Controllare i processi che occupano RAM:
   ```bash
   ps aux --sort=-%mem | head -15
   ```
   I principali consumatori tipici: Hermes gateway, t3 serve, opencode (stealth/ox-alpha), codex app-server, agy, freebuff, cloudflared.

## Mitigazioni (in ordine di preferenza)

### 1. Liberare RAM prima del render

Kill dei processi non essenziali per il render:

```bash
# t3 serve (occupa ~400 MB RSS)
kill -9 <PID_t3_serve>

# agy (Freebuff) se non in uso
kill -9 <PID_agy>

# freebuff se non in uso
kill -9 <PID_freebuff>

# Un opencode secondario (ne basta uno solo)
kill <PID_opencode_extra>
```

Dopo il kill verificare:
```bash
free -g  # available dovrebbe salire a ~2 GB
ps aux --sort=-%mem | head -8  # verificare che i processi siano morti
```

### 2. Alzare timeout per frame

Default Remotion: 30s per frame. Con RAM stretta, alzarla a 60s:

```bash
npx remotion render ... --timeout=60000
```

Nota: timeout alto senza RAM sufficiente non risolve — il processo va in OOM prima del timeout.

### 3. Transcodificare il video di intro (se persistono i problemi)

Convertire il video H.264 in formato più leggero per Chromium:

```bash
# Baseline profile, yuv420p, low bitrate
ffmpeg -y -i assets/custom-intro.mp4 -r 30 -c:v libx264 -profile:v baseline \
  -level 3.1 -pix_fmt yuv420p -preset fast -crf 23 -movflags +faststart -an assets/custom-intro.mp4
```

Valutare anche WebM/VP8 per Chrome/Chromium (più leggero in decodifica). NOTA: questa mitigazione ha effetto limitato se il problema è la RAM totale, non il codec specifico.

### 4. Se le mitigazioni sopra non bastano

Opzioni successive (da valutare con l'utente):

- **Semplificare la scena custom_intro**: rimuovere OffthreadVideo, usare una scena SVG/HTML statica con fade-in (no file video). Il video audio viene comunque inserito nel mux finale con BGM.
- **Rendering frame-by-frame** con `@remotion/renderer` (`renderFrames` + `stitchFramesToVideo`) invece del CLI `render` — permette controllo più fine e può essere più stabile su RAM limitata.
- **Aumentare swap**: se l'host lo consente, aggiungere swap file (es. 2 GB) per dare memoria virtuale al renderer.

## Cosa NON farò

- Non renderizzo il video con < 1 GB di RAM liberi. Fermo e reporto la situazione.
- Non paralleliizzo render + altre operazioni pesanti (TTS, transcode) — sequenzializzo tutto.
- Non uccido processi essenziali (Hermes gateway, opencode principale che esegue il task corrente) senza chiedere.

## Stato del problema

Questa sezione è 순간anea — aggiornata alla data del file. Il problema "video custom_intro su RAM limitata" è noto e le mitigazioni sopra sono state provate con risultati parziali. Se in futuro il video non viene generato, documentare qui il fallimento specifico e la causa esatta (es. "chrominum OOM a frame X dopo kill dei processi Y").
