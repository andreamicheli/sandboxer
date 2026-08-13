# Agent instructions

## Model routing

For implementation tickets, follow the ticket's declared difficulty and keep
implementation and review independent:

| Difficulty | Implementer | Reviewer |
| --- | --- | --- |
| Simple | Gemini 3.6 Flash via `agy` | GPT-5.6 Luna |
| Medium | GPT-5.6 Luna | Gemini 3.6 Flash via `agy` |
| Complex | GPT-5.6 Terra | GPT-5.6 Sol |

Outside ticket execution, prefer GPT-5.6 Luna for bounded implementation,
mechanical repository changes, routine verification, and other agentic work
whose requirements and acceptance criteria are already settled. Use Terra or
Sol when material product, architecture, methodology, safety, or evaluation
judgment remains. Escalate when implementation exposes an unresolved decision
instead of silently deciding it.

## Agent skills

### Issue tracker

Issues, Wayfinder maps, specs, and tickets are tracked in GitHub Issues. See
`docs/agents/issue-tracker.md`.

### Triage labels

Use the canonical Pocock triage roles mapped to the repository's GitHub labels.
See `docs/agents/triage-labels.md`.

### Domain docs

This is a single-context repository using the root glossary and system ADRs. See
`docs/agents/domain.md`.
