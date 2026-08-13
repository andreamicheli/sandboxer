# Sandboxer v0 — Wayfinder blueprint

This document consolidates the decisions made in the Wayfinder issue map. It
is the implementation contract for v0; GitHub issues remain open only while a
product decision genuinely requires active discussion.

## 1. Canonical task family

Each Match starts from the same small, pristine, synthetic notes service in
both Runner VMs. The service has deterministic health, functional-integrity,
and flag-capture tests. It contains no intentionally planted exploit.

During Blue Phase, both Competitors receive the same seed-selected **Blue
Brief**: a minor functional requirement such as constrained public export,
controlled sharing, or sanitized diagnostics. The Blue Brief states an outcome, not an
implementation. Each Competitor independently changes and defends its own
service. The Red Phase therefore targets weaknesses that emerge from those
choices, rather than a vulnerability selected by the seed.

For a best-of-3, one series seed selects three different Blue Briefs without
replacement. Minor names and parameters may vary reproducibly, but must not
create security through obscurity, change the flag, or dominate difficulty.
The v0 catalog contains 3–5 versioned Blue Briefs and is frozen for the series;
it does not require a sealed or secret catalog. Calibration must reject a Brief
if it consistently dictates one implementation, produces a
materially different success rate, or defeats functional-integrity tests.
This is a deliberately simple v0 baseline, not a permanent ceiling. After dry
runs and pilot Series, measured monotony may justify a broader catalog, richer
parameterization, or a more generative Brief-construction pipeline. Expansion
must remain symmetric and reproducible, keep the Brief weaker than the model's
implementation choices, and pass calibration for safety and comparable
difficulty before publication use.

Permitted attack effects are limited to the synthetic service and Synthetic
Flag. No attacker-supplied host command execution, internet target, real
credential, persistence beyond the Arena, or control-plane interaction is a
valid objective.

## 2. Match Telemetry and evidence bundle

Match Telemetry is an append-only JSONL stream with a versioned envelope. Every
event contains:

- `schema_version`, `match_id`, `series_id`, `event_id`, `event_type`;
- UTC wall time plus Orchestrator monotonic nanoseconds;
- emitting component, Runner/Competitor where applicable, phase and turn;
- causal parent/correlation identifiers and an idempotency key;
- public payload plus a redaction classification;
- source adapter and exact software/configuration versions;
- previous-event hash and event hash.

The Orchestrator is the authority for phase transitions, budgets, network
state, health checks, Submissions, termination, validity, and score. Adapters
may report provider usage and lifecycle events but cannot adjudicate them.
Runner observations are untrusted evidence until normalized and validated.

The minimum vocabulary covers lifecycle, phase, prompt delivery, model output,
reasoning representation, tool request/result, file mutation, service health,
network-policy transition, budget observation/exhaustion, provider retry/error,
Submission, Auditor finding/action, termination, score, artifact derivation,
redaction, correction, and publication.

Each evidence bundle contains the immutable Match specification, prompt/tool
contracts, seed commitment and post-Match reveal, full Competitor and provider
manifests, environment/image digests, raw and normalized telemetry, Inspect
log, redacted provider stream, service diffs, health evidence, score proof,
Auditor verdict, artifact manifest, checksums, and signatures. Secrets and raw
Synthetic Flags are encrypted in restricted evidence or replaced by proofs;
public artifacts contain only irreversible redactions. Raw restricted evidence
uses a declared retention period; public normalized evidence is retained with
the report. A correction creates a new artifact version and never rewrites the
original silently.

## 3. Broadcast pipeline

The video is a derived, reviewable build:

1. Validate and freeze the evidence bundle.
2. Generate a deterministic editorial timeline from scored events.
3. Reconstruct terminal panes from telemetry; do not depend on screen capture.
4. Draft two linked tracks: narrative commentary for beginners and optional
   technical callouts/report links for advanced viewers.
5. Attach every factual sentence and visual beat to one or more event IDs.
   Metaphors and strategic interpretations are explicitly typed as editorial.
6. Generate or record voice, then align captions, terminal playback, overlays,
   score state, and sound cues to a single composition clock.
7. Render a review draft, run factual, safety, accessibility, licensing, and
   pronunciation checks, approve, then render publication masters.

The recommended implementation is a data-driven Remotion project: typed scene
descriptors compiled from the editorial timeline, reusable React compositions,
and deterministic renders pinned to exact fonts/assets/tool versions. FFmpeg is
used for probing, loudness normalization, muxing, and delivery encodes. Human
review is mandatory in v0; automatic event ranking may suggest moments but may
not publish or invent causal explanations.

The visual language is strong and memorable but always frames the event as an
**experimental benchmark in a simulated capture-the-flag Arena**. A simple
metaphor layer may show defended rooms, doors, pressure, and flag movement;
terminal evidence and on-screen labels reveal what actually occurred. Captions,
audio description notes, high-contrast overlays, chapters, transcript, credits,
thumbnail, and source/license manifest ship with the master.

The episode uses a permanent 50/50 split assigned to the two public model
identities; internal Alpha/Beta aliases are never spoken or shown. After a cold
open, one variable sentence explains the simulated CTF, Blue defense, Red
attack, and the configured termination rule. Synchronized model cards show
producer, exact model/runtime, context window, weights/license status, and a
dated comparison of relevant external benchmarks. Match footage then runs
uncut at its natural duration. The Intermission is edited to roughly one minute.
There is no final opinion/analysis segment: the video ends on a factual recap
screen with the main experimental-benchmark measurements and report link.

External model metadata and benchmark results are collected ahead of production
into a versioned batch snapshot, not re-fetched for every duel. Cards may use
Artificial Analysis Intelligence, Agentic, and Coding indices plus primary
cyber evaluations such as CyberGym/CyberGym-E2E, Cybench, or NYU CTF Bench only
when both exact model configurations have comparable published results. Every
value carries source, benchmark and harness version, evaluation date, and
snapshot date. If either exact model lacks a comparable result, the entire
benchmark field is omitted graphically; a related model or different harness is
never substituted.

Uncut Match footage uses a coordinated two-voice commentary track. The primary
play-by-play voice explains immediate events didactically and may offer sparse,
bounded opinions. A less frequent analyst voice provides broader strategic
context. A single dialogue scheduler sees both model timelines, balances
attention, assigns each line to one voice, forbids overlap, and inserts natural
pauses. Silence is an intentional option rather than a gap to fill. Commentary
may never obscure decisive terminal cues or pretend that simultaneous actions
occurred sequentially.

Speech synthesis uses Google's `gemini-3.1-flash-tts-preview` through a replaceable TTS
adapter and a control-plane-only API key. Two pinned prebuilt voices are mapped
to the commentary roles and rendered in bounded scene blocks; the deterministic
dialogue schedule, not the audio model, controls order and non-overlap. Each
block records script/model/voice/style configuration and audio hashes. Automated
QA checks transcript alignment, duration, clipping/noise, unintended silence,
speaker swaps, and technical-name pronunciation, allowing only the failed block
to be regenerated. Model or voice drift fails preflight rather than silently
changing the episode's sound. Because the selected endpoint is a preview,
availability and voice stability are hard preflight gates and any fallback must
be explicitly approved rather than silently substituted.

Sanitized terminal replays are the primary Match visual. The brand uses an
original, restrained retro-futurist hacker system: deep navy-black foundations,
electric cobalt system accents, bitmap display typography paired with legible
monospace and sans-serif faces, thin technical borders, subtle CRT/dither/grid
texture, restrained glow, and generous editorial spacing. It borrows no Hermes
Agent assets or layouts and avoids neon-heavy cyberpunk and dense HUD noise.

Each terminal uses a subtle model-colored dark gradient while normal text stays
neutral: for the initial pair, a very dark navy/blue DeepSeek surface and very
dark brown/orange MiMo surface. Accent colors mark the frame, prompt, cursor,
budget and callouts without recoloring all code. A persistent dual-terminal
split and a small Wayfinder route marking phase/flag movement form the distinct
Sandboxer motif. All foreground/background pairs are contrast-tested; names,
icons and position accompany color. Glitch, scanlines and glow never obscure
terminal evidence.

Remotion is the deterministic compositor and FFmpeg performs final loudness,
muxing, and delivery encoding. Selected Remocn components may be copied into the
project for restrained type reveals, charts, progress, grain, and dither/ASCII/
caret transitions. Each is vendored at a pinned revision and reviewed for
license, deterministic rendering, accessibility, and unused behavior. Remocn's
generic terminal simulator is not used for Match evidence: the sanitized
Sandboxer terminal renderer remains custom and authoritative.

The terminal camera keeps the permanent 50/50 split and authentic real-time
scrolling. It does not zoom one Competitor over the other; restrained gutter
markers may identify decisive lines without hiding evidence.

A global, versioned **Concept Explainer Ledger** prevents repetitive educational
cards across published episodes. When a sanitized agent action or approved
commentary first introduces a cybersecurity or operating-system concept, a card
may show its name, a plain-language explanation, an explicitly illustrative
metaphor, and why it matters at that moment. Episodes target 3–6 cards and may
never exceed 10. Cards appear one at a time in non-decisive readable windows.

Canonical concept IDs track status, concept version, first published episode and
timecode, explanation hash, sources, and related concepts. Draft and batch-lab
renders do not consume first use. Publication atomically reserves and commits
ledger entries so queued videos cannot duplicate them. Only a material concept
revision creates renewed eligibility; later episodes may mention the concept but
do not repeat the full card. Candidate detection may use models, while first-use,
count, and publication rules remain deterministic and human-reviewed.

Explainer cards emerge from the center divider as a floating overlay centered in
the upper area. They do not resize the terminals or dim/blur the rest of the
frame. Their footprint is bounded, and while visible the renderer keeps active
or decisive lines and callouts outside a protected top-center safe zone. Word
count and height are limited for quick reading; longer material belongs in the
report glossary, so an uninterested viewer can continue following both terminals.

## 4. Result Report and website

Each best-of-3 produces one canonical **Series Result Report**. It is a single,
linkable document that contains the overall comparison and a complete technical
chapter for every valid Match, plus a separate incident appendix for invalid
attempts. It does not split the narrative and technical record across distinct
reports.

The document uses progressive disclosure: an opening result and narrative for a
general audience; a cross-Match strategic analysis; individual Match chapters;
methodology and limitations; and an evidence/reproducibility appendix. It
contains:

- winner and decisive rule, followed by a beginner-facing narrative summary;
- an explicit experimental/simulated-CTF disclaimer;
- Competitor producer + model names and complete technical manifests;
- equivalence assessment for prompts, tools, budgets, provider accounting, and
  opportunity;
- protocol, Blue Brief, Arena, Harness, adapter, versions, seed and role order;
- phase-by-phase timeline, interview excerpts, decisive events and score proof;
- raw comparable measurements for output tokens, turns, tools, health,
  Submissions and errors, with non-comparable categories clearly excluded;
- latency, retries, refusals, rate limits and provider/infrastructure faults as
  context rather than competitive budget;
- observed facts separated from inferred strategy and editorial metaphor;
- Auditor verdict, anomalies, confounders, limitations and reproducibility;
- hashes and links for public telemetry, schema, prompts, code, replay, video,
  paper and restricted-artifact attestations;
- publication/correction history and machine-readable JSON.

The report interprets Competitor behavior when telemetry supports it: defensive
posture, offensive pressure, exploration, verification, adaptation, risk
tolerance, resource allocation, repeated failure modes, and differences between
what a model said in the Interview and later did in Red Phase. Every analytical
statement is typed as one of:

1. **Observed** — directly supported by cited Match events or artifacts.
2. **Derived** — mechanically computed from declared measurements.
3. **Interpreted** — the report's evidence-linked reading of behavior, with
   plausible alternatives stated where material.
4. **Hypothesis** — a broader pattern worth testing, never a general model claim.

A single episode may support a Match-specific interpretation but not a stable
model trait. Cross-Match language states its sample size and becomes stronger
only with repeated compatible evidence. External context may be cited when it
helps explain or compare a pattern, but it never substitutes for Match evidence.
Sources must be verified against primary material when available, archived with
access date/version, and linked at claim level. A research subagent may discover
or challenge sources; a separate verification pass must confirm every source and
that it actually supports the sentence before publication.

Routine derivation uses complexity-based model routing. Deterministic code
produces facts and measurements; Gemini 3.6 Flash and Codex Luna are default
workers, with Codex Terra and Sol reserved for complex analysis and high-impact
synthesis. The public Series Result Report deliberately runs the full diverse
review chain every time: Gemini evidence triage and clustering, Luna web/source
discovery, Sonnet behavioral analysis, Terra skeptical and claim/source review,
and Sol final synthesis/review, followed by human approval. A source discovered
by Luna is never self-approved; verification runs in a separate context and is
checked again by the human reviewer. Each invocation records
model/version, prompt, inputs, structured output, disagreement and escalation
reason. Command Code models are excluded while Command Code supplies the
benchmark Competitors. The canonical report is English only; no official
translation is maintained.

The site is Match-first, responsive, keyboard accessible, caption/transcript
friendly, printable, and supports reduced motion. v0 may show series records and
filters, but no global ranking, Elo, season table, or generalized model card.
Cross-Match summaries are descriptive and require compatible protocol/schema
versions. Superseded reports remain addressable and visibly point to the
correction.

## 5. Short methodology paper

The v0 paper is a concise technical report for AI-evaluation practitioners and
an informed general audience. Its structure is: motivation and claim;
related context; threat model and containment; task and Match protocol;
Competitors and equivalence; telemetry and Auditor; scoring and best-of-3;
pilot results; limitations/threats to validity; reproducibility; ethics and
responsible publication; future validation.

The evidence table maps every reported result to a public artifact hash and
states whether it is observed, derived, or interpreted. An appendix includes
schemas, prompts, tool contracts, version manifests, seed procedure, exclusions,
failure codes, and replay commands. The first release should be a versioned
repository preprint with a stable archive identifier; choosing a journal,
workshop, or formal review venue is deliberately deferred until v0 evidence
exists.

EnIGMA and NYU CTF Bench are retained as report-writing references as well as
task-design research: they provide useful precedents for describing interactive
tool effects, CTF task structure, trajectory evidence, observed-versus-claimed
behavior, dataset validation, and limitations. They are contextual sources, not
evidence that Sandboxer measures the same construct or supports real-world cyber
claims.

## 6. Match Auditor

The trusted Auditor is independent of both Competitors and the editorial layer.
Deterministic controls are authoritative for identity, symmetry, budget
categories, allowlisted tools/network, telemetry continuity, resource limits,
Submission verification, forbidden targets, credential access, privilege or
sandbox-escape indicators, persistence, and control-plane reachability.

An independent LLM reviewer may assess task sensibility, semantic equivalence,
ambiguous novel risk, and whether interpretations are supported. It may warn,
request pause/abort/invalidation, or escalate, but cannot weaken a rule, fill in
missing measurements, edit evidence, or permit a blocked action.

Actions are `warn`, `pause`, `abort`, `invalidate`, `disqualify`, `quarantine`,
and `escalate`, each with a versioned reason code and evidence references.
Platform/provider faults invalidate; deliberate Competitor violations may
disqualify. An out-of-Arena safety signal fails closed. Provisional validity is
continuous; final validity is signed only after telemetry closure and teardown.
Invalid Matches stay private by default and may be published only as a manually
approved, clearly labelled incident without comparative claims.

## 7. End-to-end orchestration

The Orchestrator owns a persisted state machine:

`DRAFT → APPROVED → PROVISIONING → PREFLIGHT → BLUE → INTERVIEW → RED →
FINALIZING → AUDIT → DERIVING → REVIEW → PUBLISHED → RETIRED`.

Failure states are `PAUSED`, `ABORTED`, `INVALID`, `QUARANTINED`, and
`PUBLICATION_REJECTED`. Every transition is compare-and-set, idempotent, emits
telemetry, and has explicit retry and cleanup semantics. Match execution is
never retried selectively; a new attempt gets a new Match ID and follows the
predeclared series policy.

The trusted control plane provisions two fresh private Runner VMs, calls model
providers, enforces budgets, receives one-way telemetry, and destroys resources.
Runners contain only the toy service and narrow tools. Publication workers read
a frozen, redacted bundle and cannot reach live Runners or provider credentials.
The Orchestrator is neither network-addressable nor a valid target from the
Arena.

v0 automates provisioning, preflight, phase control, budgets, telemetry closure,
scoring, audit checks, teardown, report data, replay, and draft video rendering.
Normal mode requires one human approval before Series execution/spend and a
separate approval before public release.

An explicitly isolated `batch-lab` mode may disable intermediate approval gates,
queue multiple test Match or Series specifications, execute them serially, wait
for provider capacity or subscription-credit renewal, and continue through
draft video generation. It retains every safety, validity, accounting, and
teardown gate. Its artifacts are marked `TEST / NOT FOR PUBLICATION`, and it
cannot publish or promote evidence without a new supervised workflow. Waiting
uses bounded backoff, a deadline, and a cost ceiling; authentication failures,
catalog drift, policy incompatibility, and safety failures block rather than
wait. Production rejects approval-bypass flags by construction.

Queueing, cost ceilings, per-resource TTLs, cancellation, quarantine, and
reconciliation jobs prevent orphaned infrastructure. Local dry-run uses fake
providers; production and republish modes share the same artifact contracts.

The v0 operator interface combines an authoritative CLI for mutating actions
with a mobile-first, Internet-reachable but authenticated read-only dashboard.
The dashboard consumes a sanitized projection and shows queue, phase, budget
progress, provider-capacity waits, Auditor alerts, and artifact status. It has
no Runner connectivity and never exposes raw prompts/outputs, private reasoning,
Synthetic Flags, credentials, or Arena endpoints. Anonymous pages are reserved
for already-published results; web-based control actions are deferred.

## 8. Release gates

A Match may start only when its immutable specification is approved; exact
models and versions resolve; comparable categories exist or a symmetric
fallback excludes them; fake-provider dry-run passes; isolation, deny-by-default
egress, Orchestrator unreachability, health tests, telemetry sink, clock checks,
budgets, credits/cost ceiling, TTL, and cleanup are verified.

A Match result may finalize only after both terminal conditions are resolved,
all authoritative events are present, score recomputation matches, the Auditor
signs final validity, and both Runners are destroyed or quarantined. Missing or
asymmetric material evidence invalidates the Match.

A report/video/paper may publish only from the same frozen valid bundle after
schema, link/hash, factual traceability, redaction, safety, claim-language,
accessibility, licensing, and human editorial approval checks. Publication is
atomic through versioned artifacts and a reversible pointer; rollback never
deletes evidence or silently replaces history.

The tagged v0 release additionally requires deterministic fixtures, a full fake
end-to-end run, one production rehearsal, recovery/teardown drills, documented
operator procedures, dependency/image manifests, and a signed known-limitations
statement.

## 9. Command Code provider boundary

Command Code `1.15.1` is a conditional provider boundary for the next pilot,
not part of Competitor identity. The first intended series is
`deepseek/deepseek-v4-pro` versus `xiaomi/mimo-v2.5-pro`; DeepSeek Flash is the
calibration baseline, Laguna the free smoke model, Muse the fallback, and
GPT-5.6 Luna a later closed-model reference.

The adapter runs the globally resolved executable in an Orchestrator-owned
disposable directory with NDJSON, no persistent session, no auto-update, no
skills, skipped onboarding, and fail-closed permissions. It never uses yolo,
trust, or automatic acceptance. Native CLI tool execution is rejected; future
tools cross only the Sandboxer Runner bridge. Provider credentials and session
state remain in the control plane.

Competitive accounting uses a common total-output-token budget, plus symmetric
turn and tool budgets. Cache usage is excluded. Reported input tokens are
evidence only because fixed CLI context and caching are not comparable. Output
budget checks are observed at response boundaries; a response that crosses the
cap is retained as evidence but cannot start another turn. The same overshoot
rule applies to both Competitors.

### Deterministic provider preflight

Preflight records the CLI binary hash/version, adapter and schema versions,
redacted authenticated-account fingerprint, live catalog snapshot, exact model
presence, advertised context/pricing, entitlement/credits and concurrency/rate
limits when observable, ZDR/session flags, output categories, and capability
probe results. It then runs a credential-free fake-CLI contract suite.

Hard rejection reason codes cover missing/aliased model, catalog drift,
insufficient entitlement or credits, unknown rate/concurrency limits, asymmetric
usage categories without the declared fallback, unsupported headless/NDJSON or
session controls, tool-boundary failure, adapter/version drift, failed dry-run,
and absent live-call approval. The redacted snapshot and its hash enter the
evidence bundle. A changed catalog never silently substitutes a model.

Before a valid Match, the remaining operational gates are a multi-turn
continuation test, controlled Runner-tool mediation, cancellation/failure
fixtures, pair concurrency observation, and written confirmation that repeated
synthetic CTF evaluation complies with provider and upstream-model policies.
Until then, Groq remains the live pilot path.
