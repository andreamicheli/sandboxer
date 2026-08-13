# Command Code adapter prototype

This prototype validates the process and NDJSON boundary proposed in Wayfinder
issue #21. It is not wired into the pilot and makes no live provider calls in
its test suite.

The adapter:

- invokes an explicit, pinned Command Code executable;
- always requests non-persistent, non-updating, skill-free headless mode;
- parses stdout one NDJSON line at a time;
- rejects malformed frames, duplicate/missing results, and unexpected tools;
- preserves documented process exit classes;
- enforces an outer timeout and terminates the child process;
- exposes usage categories without pretending they are already comparable.

Run from the repository root:

```bash
pilot/.venv/bin/python -m pytest prototypes/command_code_adapter/tests -q
```

The fake CLI fixture covers adapter behavior without authentication, credits,
or network access. Runner tool mediation and valid-Match approval remain gated.
