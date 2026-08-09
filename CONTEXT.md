# Sandboxer

Sandboxer è un progetto per osservare e raccontare competizioni cyber controllate tra modelli AI. Il dominio separa rigorosamente la partita, l'infrastruttura di contenimento, la valutazione e il livello editoriale.

## Competizione

**Match**:
Una singola competizione tra due Competitor dentro un'Arena usa-e-getta, articolata in Blue Phase e Red Phase e conclusa da un risultato verificabile.
_Avoid_: Run, esperimento, benchmark

**Competitor**:
La combinazione dichiarata di modello, configurazione del modello e Model Adapter che partecipa a un Match. Il nome pubblico del Competitor deve rendere riconoscibile il modello effettivo.
_Avoid_: Alpha, Beta, agent

**Blue Phase**:
La fase iniziale in cui ogni Competitor può osservare e modificare soltanto il proprio Runner per preparare la difesa.
_Avoid_: Setup phase, preparation

**Red Phase**:
La fase competitiva in cui i Runner ricevono connettività limitata verso l'Arena condivisa e ciascun Competitor tenta l'obiettivo offensivo preservando il proprio servizio.
_Avoid_: Attack mode, live phase

**Synthetic Flag**:
Un segreto effimero generato per il Match, privo di valore esterno, la cui submission dimostra una cattura valida.
_Avoid_: Credential, secret reale

**Submission**:
La registrazione verificata di una Synthetic Flag avversaria da parte di un Competitor.
_Avoid_: Claim, output

## Esecuzione controllata

**Arena**:
L'ambiente di rete temporaneo e delimitato in cui si svolge un Match e che viene distrutto o reimpostato al termine.
_Avoid_: Sandbox, server

**Runner**:
L'ambiente isolato e senza credenziali che espone al Competitor soltanto il toy service e i tool consentiti.
_Avoid_: Container, VM, agent

**Orchestrator**:
Il componente fidato esterno ai Runner che gestisce fasi, chiamate ai provider, limiti, rete dell'Arena, logging e verifica del risultato.
_Avoid_: Referee, host script

**Harness**:
Il protocollo di esecuzione condiviso che presenta osservazioni e tool ai Competitor e raccoglie le loro azioni. Inspect è l'implementazione iniziale, non parte dell'identità del Competitor.
_Avoid_: Model, provider

**Model Adapter**:
Il confine che traduce il protocollo del Harness nelle chiamate a uno specifico provider o runtime mantenendo invariato il Match.
_Avoid_: Harness, model

## Evidenza e pubblicazione

**Match Telemetry**:
Il flusso autorevole e timestampato di transizioni di fase, turni, azioni, errori, token, salute, submission e punteggio prodotto durante un Match.
_Avoid_: Transcript, console output

**Replay**:
Una ricostruzione deterministica o temporalmente fedele di un Match derivata dalla Match Telemetry.
_Avoid_: Screen recording, recap

**Result Report**:
La pagina pubblicabile che identifica i Competitor e rende comprensibili cronologia, risultato, metodologia e limiti di un Match.
_Avoid_: Leaderboard, paper

**Broadcast Layer**:
La trasformazione editoriale della Match Telemetry in telecronaca, annotazioni visuali, metafore accessibili e video destinato al pubblico.
_Avoid_: Benchmark, replay

**Showcase Evaluation**:
Una valutazione esplorativa progettata per produrre evidenza interessante e osservabile senza rivendicare la validità di un benchmark scientifico maturo.
_Avoid_: Scientific benchmark, leaderboard definitiva
