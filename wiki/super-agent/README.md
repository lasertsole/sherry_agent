# Super Agent

A side-process orchestrator that turns messages from GitHub issues and Feishu
topics into tracked work, delegates it to Sherry worker subagents, and reports
back to the originating channel. It owns its own SQLite state and talks to
Sherry only through narrow loopback seams.

## Why a separate process

Sherry is the execution unit; the super agent is the manager. Keeping them
apart means the manager's SQLite state, tmux session and hourly check survive a
Sherry restart, and a Sherry outage degrades to "queue Triggers, lose nothing"
instead of killing the workflow. The super agent never imports Sherry core
code — it calls `/internal/superagent/*` loopback routes.

## Architecture

```
GitHub / Feishu  →  Sources  →  Engine  →  Effects  →  Sherry workers
                                  ↓          ↓
                             SQLite Store   Outbox → channel replies
```

The Engine is the single writer of Triggers, Threads, Decisions and Tickets.
Every command is one SQLite transaction; adapters (Sources, Executor, Outbox,
Assessor) are async and communicate with the Engine through narrow ports.

- **Sources** translate external events into Triggers and send replies back.
- **Engine** runs five parts in one direction: Intake → Evals → Tickets →
  Assignments → Replies.
- **Effects** is a durable, coalescing, retrying queue — every side effect
  survives a restart.
- **Outbox** is the only writer of channel Receipts, with ordering and
  idempotency.
- **Passes** runs due Effects then due Evaluations every two seconds.

## Authorization and assessment

A Policy decides whether a Trigger is explicit or inferred, using an admission
table and an accept mode of `off`, `observe` or `auto`. Inferred work is only
accepted when auto mode and rules are both enabled. Assessment is delegated to
a dedicated Sherry session that reads a bounded thread window and returns a
Decision; HITL answers that decline fail closed into a blocked Ticket.

## Setup and lifecycle

`superagent.jsonc` holds the state directory, Sherry loopback URL, panel port
and Source configs. `scripts/superagent` provides `start`, `stop`, `restart`
and `status` over a dedicated tmux session, with exit codes 0 (healthy),
1 (not running / runtime error), 2 (part error) and 3 (setup incomplete).
An hourly check reads those codes and only restarts on 1. See
[deployment](deployment.md) for the full runbook.

## Panel

A Nuxt `/superagent` page polls the loopback panel API and exposes six tabs:
Overview, Sources, Triggers, Evals, Execution and Settings. The API is
loopback-guarded (host + origin + a request header) so a page on another
origin cannot drive it. Settings are revisioned; a stale save is rejected.

## Tests and gates

`tests/superagent/` mirrors the package with unit tests for the domain model,
Engine, Policy, Effects, Outbox, Sources and panel, plus end-to-end chains:
GitHub issue → Ticket → PR and Feishu mention → Ticket (both with mocked CLI
and worker). Static gates are `ruff`, `basedpyright` and `lint-imports`
layered contracts; doc gates are parity, links and language.
