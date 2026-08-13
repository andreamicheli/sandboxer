# Issue tracker: GitHub

Issues and specs for this repository live in `andreamicheli/sandboxer` GitHub
Issues. Prefer the connected GitHub app for supported reads and writes; use
`gh` from this checkout where native sub-issue, dependency, or label operations
are not exposed by the connector.

## Conventions

- Publish specs and implementation tickets as GitHub issues.
- Read an issue together with its labels and resolution comments.
- Apply and remove labels without replacing unrelated labels.
- Pull requests are not a triage request surface.

## Wayfinding operations

- A map is an issue labelled `wayfinder:map`.
- Tickets are GitHub sub-issues where available. Otherwise, the map task list
  and a `Part of #<map>` pointer are the fallback relationship.
- Ticket labels are `wayfinder:research`, `wayfinder:prototype`,
  `wayfinder:grilling`, or `wayfinder:task`.
- Prefer native GitHub dependencies for blocking edges; use an explicit
  `Blocked by:` line only when native dependencies are unavailable.
- Claim an open frontier ticket by assigning it before beginning work.
- Resolve it with an answer comment, close it, and append a named link plus a
  one-line gist to the map's `Decisions so far` index.

## Skill operations

- When a skill says to publish to the issue tracker, create a GitHub issue.
- When a skill says to fetch a ticket, read its body, labels, and comments.
