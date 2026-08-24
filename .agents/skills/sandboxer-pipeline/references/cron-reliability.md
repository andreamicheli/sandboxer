# Cron reliability on this host — silent failure modes (learned 2026-08-22)

## Symptom

A previously working Hermes cron (`hype daily draft`, job f1af004bbe41) stopped
delivering with no user-visible error. Two distinct failure stages:

1. **Silent skip via config-drift guard.** Crons snapshot `provider`/`model`
   at creation. After the global Hermes model config changed
   (orcarouter/free → nous/ox-alpha), runs failed with:
   `RuntimeError: Skipped to prevent unintended spend: global inference config drift`
   This error is NOT surfaced to the user — it lives only in the executions DB.
2. **Job vanished from jobs.json** entirely (probable gateway restart loss).
   After that, even the silent failure disappeared.

## Diagnosis path

```bash
# What jobs exist now?
python3 -c "import json; print([j['name'] for j in json.load(open('/home/ubuntu/.hermes/cron/jobs.json'))['jobs']])"

# What actually ran / failed? (the authoritative history)
python3 - <<'EOF'
import sqlite3
c = sqlite3.connect('/home/ubuntu/.hermes/cron/executions.db')
for row in c.execute('select job_id, started_at, status, error from executions order by rowid desc limit 10'):
    print(row)
EOF

# Draft output files (proof of last good run)
ls -la ~/.hermes/cron/output/ ~/.hermes/campaigns/*_drafts/
```

## Fix / prevention

- Recreate the cron after any global model switch so its snapshot matches the
  new config. The recreated job (no stale snapshot) runs fine.
- Check `executions.db` whenever a user reports "the scheduled thing never
  arrived" — `cronjob(action='list')` only shows jobs that still exist.
- A missing job from `jobs.json` means recreation, not debugging.

## Related state (as of 2026-08-22)

- `hype daily draft` recreated as job `9b5bdfc4c033` (0 8 * * *, telegram,
  draft-only; Andrea approves and posts manually — X API Free is read-only).
- `opencode-watchdog` job `2a1191d5f1c5` (every 10 min): reports exited
  background opencode processes not yet reported to Andrea. Exists because a
  finished 2h opencode job once went unreported; Andrea expects unprompted
  completion reports. notify_on_complete=true on every background launch is
  mandatory regardless of this cron.
- Campaign contract: `~/.hermes/campaigns/hype-2026-08.json` (4-week arc,
  last_post tracking, calendar); drafts in `~/.hermes/campaigns/hype-2026-08_drafts/`.
