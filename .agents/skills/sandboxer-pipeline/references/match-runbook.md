# Live match runbook (KVM backend)

Verified 2026-08-22 on the Laguna vs Muse rehearsal path.

## Prerequisites check (before launching)

```bash
ls -t /var/lib/sandboxer/images/*.qcow2 | head -3     # latest runner image (r67 as of 2026-08-22)
command-code --version                                 # bridge binary v1.31.x at /usr/local/bin/cmd
sudo -n true                                           # match orchestration requires root
free -g                                                # 2x QEMU runners need headroom; render preflight reads RAM too
```

Smoke-test the model bridge before burning a match slot (~20s):

```bash
/usr/local/bin/cmd -p "Respond with exactly: CMD_OK" --model stealth/ox-alpha \
  --output-format json --no-session --no-auto-update --no-skills \
  --skip-onboarding --permission-mode dont-ask
```

Expect a final `"finalText":"CMD_OK"` result line. This is the exact argv the
`CmdAgent` adapter builds (`sandboxer_v0/agents.py`), so success here means the
match adapters will reach ox-alpha.

## Launching (job_runner, detached)

`job_runner launch` REQUIRES `--log` (fails without it). Run from `pilot/`,
as root, with paths relative to `pilot/`:

```bash
cd pilot && sudo .venv/bin/python -m sandboxer_v0.job_runner launch \
  --job-id <match-id> --log logs/jobs/<match-id>.log -- \
  .venv/bin/python scripts/run_command_code_match.py \
  --image /var/lib/sandboxer/images/runner-alpine-3.24.1-r67.qcow2 \
  --profile /var/lib/sandboxer/images/runner-alpine-3.24.1-r67.profile.json \
  --match-id <match-id>
```

Status / stop (no root needed for status):

```bash
.venv/bin/python -m sandboxer_v0.job_runner status <match-id>
.venv/bin/python -m sandboxer_v0.job_runner stop <match-id> --timeout 10
```

Healthy startup: within ~30s there are exactly 2 `qemu-system-x86_64`
processes (one per runner) and a match dir appears under
`/var/lib/sandboxer/runners/<match-id>/`. The job log may stay empty during
the match; evidence lands in `/var/lib/sandboxer/evidence/`.

## Telegram watchdog cron for a live match

Hermes cron minimum interval is 1 minute (NOT 30s — the scheduler rejects
sub-minute specs). Pattern that works:

- schedule `* * * * *`, `repeat: 60` (self-extinguishes after an hour)
- prompt instructs: check job status + `ps aux | grep qemu` + tail newest logs;
  report concise Italian status to Telegram; ALLERTE-bold on grave conditions
  (orphan QEMU, OOM, repeated errors); `[SILENT]` when nothing changed
- include a dedup instruction ("only report if state changed since last tick")
  so the channel is not spammed every minute with identical status

## Custom intro assets (user-supplied AI videos)

User provides Google Labs Flow share links
(`https://labs.google/fx/tools/flow/shared/video/<id>`). Download recipe:
fetch the share page HTML, extract the og-video API URL
`https://labs.google/fx/api/og-video/shared/<id>` — it responds directly with
`video/mp4`. Save under `video/public/assets/`, verify with `ffprobe`
(duration/fps must be known for the manifest frame count).

Renderer integration (2026-08-22): scene type `custom_intro` replaces
`cold_open` outright (user decision: net replacement, not sequential).
Duration = round(8*fps) frames for an 8s clip. No commentary budget exists
for the scene — the schedule authority drops unknown-scene lines, which is
the intended silence. A future series will map one intro video per matchup
from a user-provided list; keep selection deterministic by pair slug.
