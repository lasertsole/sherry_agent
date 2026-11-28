# Super Agent deployment

The super agent runs as its own process in its own tmux session, independent of
the Sherry profile you work in. This page is the runbook.

## Configuration

Copy `superagent.jsonc` next to the checkout or point `DSH_SUPERAGENT_CONFIG`
at it. Every value can be overridden by an environment variable (the env wins):

| Key | Environment variable | Default |
|-----|----------------------|---------|
| `state_dir` | `DSH_SUPERAGENT_STATE_DIR` | `.artifacts/superagent` |
| `sherry_backend_url` | `DSH_SUPERAGENT_SHERRY_URL` | `http://127.0.0.1:3081` |
| `port` | `DSH_SUPERAGENT_PORT` | `3082` |
| `shared_secret` | `DSH_SUPERAGENT_SECRET` | empty |
| `mcp_transport` | `DSH_SUPERAGENT_MCP_TRANSPORT` | `stdio` |

The state directory holds `orchestrator.sqlite`, a single-instance lock,
`worker-bin` shims, `worker-gitconfig` and per-Ticket worktrees. A second
process opening the same state directory is refused.

## Worker environment

Setup writes managed shims into `worker-bin` (for `gh`, optional `lark-cli`,
and any blocked commands) and a worker-only `config` into `worker-gitconfig`.
Worker processes get `PATH` prefixed with `worker-bin`, `GIT_CONFIG_GLOBAL`
pointed at the managed config and `GIT_CONFIG_SYSTEM` disabled. Credentials
(`GH_TOKEN`, `GITHUB_TOKEN`) are never copied into a worker environment. Any
foreign file in either managed directory stops startup, fail closed.

## Service commands

```bash
scripts/superagent start          # start the tmux session, wait up to 60s for ready
scripts/superagent status         # report health with an exit code
scripts/superagent restart        # stop then start
scripts/superagent stop           # send Ctrl-C and write a stop marker
```

Options: `--profile NAME`, `--checkout DIR`, `--port N`, `--respect-stop`.
`start` refuses to duplicate a running session; `--respect-stop` makes a
restart honour a manual stop marker.

## Exit codes

| Code | Meaning | Hourly-check action |
|------|---------|---------------------|
| 0 | Healthy / action succeeded | Silence |
| 1 | Not running or runtime error | Restart with `--respect-stop` |
| 2 | Running but a part reports error | Report, do not restart |
| 3 | Running but setup incomplete | Report setup needs, do not restart |

## Hourly check

Run the checker from a different Sherry profile so it never checks itself. The
full prompt is in `superagent/HOURLY_CHECK.md`. On exit 1 it restarts once with
`--respect-stop`; if that still fails it reads the newest
`.artifacts/superagent-*.log` and this page's troubleshooting section.

## Troubleshooting

- **"did not come up"** — the process exited during startup; read the newest
  log. A locked state directory means another process owns it.
- **exit 3, `missingCli` / `badCredentials`** — install `gh` and export a token;
  setup checks presence only, it never reads the value.
- **exit 3, `unmanagedFiles`** — a foreign file appeared in `worker-bin` or
  `worker-gitconfig`; remove it so setup can own the directory again.
- **exit 2, a part in `errors`** — read the part's `detail`; the HealthBoard
  only reports an error after three consecutive failures and clears on success.
- **workers not spawning** — confirm the `/internal/superagent/*` routes are
  reachable and the main session exists via `session/exists`.

## Restart resilience

Pending Effects, undelivered Inputs, open Tickets and outbox Receipts all live
in SQLite. After a kill and restart, in-flight intents replay, running
Assignments get a recovery note, interrupted turns are healed and idle Tickets
remain open. Only in-memory Sherry session state is rebuilt.
