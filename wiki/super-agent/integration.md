# Super Agent integration with Sherry

The super agent is a separate process. It never imports Sherry core code and
Sherry never imports the super agent. Everything crosses one of two narrow
seams: HTTP loopback routes on the Sherry side, and MCP tools on the
super-agent side.

## Loopback routes

Sherry exposes `/internal/superagent/*` from `server/trigger/http/superagent.py`:

- `spawn` / `cancel` — start or cancel a worker subagent of the persistent main
  session (`requester_session_key=superagent-main`).
- `session/ensure` / `session/exists` — create or verify the router session.
- `session/inject` — deliver an assessment candidate to the router session.
- `session/add_messages` / `session/truncate` / `session/heal` — deliver queued
  Inputs, trim on retry, and heal an interrupted turn after a restart.
- `session/state` / `session/jobs` / `session/kill_job` / `session/trajectory` —
  read state, manage background jobs and read trajectory events.
- `/internal/test` — the P-Spike probe that confirmed bidirectional HTTP.

A hook registry lets tests replace any handler without touching Sherry code.
The Sherry gateway already permits origin-less loopback calls; an explicitly
wrong token is rejected.

## MCP tools

`superagent/mcp/server.py` exposes the `orchestrator_*` tools as MCP tools, so
Sherry loads them through `mcp.servers` and `build_main_tools()` stays
untouched. Main sessions get the full tool set; workers get a reduced set and
are scoped to their own Ticket at the tool boundary — a worker calling a tool
with another Ticket's id is rejected.

Declare the server in `sherry.jsonc`:

```jsonc
"mcp": {
  "servers": {
    "superagent": {
      "command": "python",
      "args": ["-m", "superagent.mcp"]
    }
  }
}
```

## Why workers are subagents, not independent processes

`spawn_subagent_direct` requires a parent session key, and the continuation
latch that lets a long turn keep running is reset by the child's completion
drain. Workers are therefore subagents of the persistent main session. This
was validated in P-Spike before the Executor was built.

## Degraded mode

If Sherry is down, the main session is gone and the super agent cannot route,
spawn or run turns. It does not treat that as a worker failure: it stays in
degraded mode, keeps accepting Triggers into SQLite and resumes when Sherry
returns. No work is lost during the outage.

## Restart ownership

The super agent owns the durable state: pending Effects, undelivered Inputs,
open Tickets and Receipts. Sherry owns only in-flight session state. On restart
the super agent replays in-flight intents and heals interrupted turns through
the loopback routes; idle Tickets are not lost and outbox Receipts continue
delivering.
