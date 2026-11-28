# Hourly check cron prompt

Run this from a **different Sherry profile** than `superagent` — the checker must
not be the process it is checking. Paste the block below as the scheduled prompt.

```text
Check superagent: run `scripts/superagent --profile superagent status` in <checkout>.
Exit 0: do nothing and say nothing.
Exit 2: do not restart; tell me each part under `errors` with its detail, unless already told.
Exit 3: do not restart; tell me what `setup` says setup needs, unless already told.
If it reports manually_stopped: true, leave it stopped.
Otherwise (exit 1) run `scripts/superagent --profile superagent --respect-stop restart`,
which exits the same way: 0 means recovered, say nothing; 2 and 3 reported as above;
if it exits 1 or fails otherwise, read the newest .artifacts/superagent-*.log and the
Troubleshooting section of wiki/super-agent/deployment.md, fix what you can, and tell me.
```

## Exit-code contract

| Code | Meaning | Checker action |
|------|---------|----------------|
| 0    | Healthy / action succeeded | Silence |
| 1    | Not running, or runtime error after action | `--respect-stop restart`, then read logs if still 1 |
| 2    | Running but a non-runtime part reports error | Report `errors`, do **not** restart |
| 3    | Running but setup incomplete | Report `setup` needs, do **not** restart |

`--respect-stop` preserves a stop marker written by `stop`. Only `stop` writes the
marker, `start` clears it, and `restart` leaves it alone — so a deliberate manual
stop survives an hourly check.
