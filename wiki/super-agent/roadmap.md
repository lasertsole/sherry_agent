# Super Agent roadmap

The port lands in six milestones. Each has a concrete acceptance gate; a
milestone is not called done until its gate is green.

## P-Spike — Sherry seam validation

Verifies the three load-bearing assumptions before any real work: a completed
worker subagent resets the main session's continuation latch; a subagent can
call an MCP tool; and the `/internal/test` loopback route carries a request both
ways. If any fails, the integration design is revisited rather than proceeding.

## P0 — Skeleton, domain and Store

Establishes the `superagent/` layered package, the full domain model (enums,
dataclasses, shared predicates), deterministic IDs, reason/error/stage tables,
the in-memory repositories, the SQLite schema-11 Store with nested transactions
and the 10→11 migration. Gate: the Engine runs domain rules on memory repos,
and MemoryRepositories and SQLite Store roll back identically.

## P1 — Engine parts, Effects and Policy

Implements the five Engine parts, the commit-driven Effects queue, the Outbox,
the two-second Pass scheduler, the HealthBoard and the Policy gate/revalidate
logic. Gate: a Ticket lives its whole lifecycle on a simulated host, from
Trigger to accept to Assignment to a ready Report and Ticket end.

## P2 — GitHub Source and Executor

Adds the Source registry and host, shared polling, the GitHub Source (notification
polling, Trigger translation, PR following, three-state channel send), and the
SherryExecutor with worker slots, background jobs, restart resume and transient
retry. Gate: a GitHub issue becomes a Ticket and drives a PR in a mocked CI.

## P3 — Feishu, panel and prompts

Adds the Feishu Source (live events, history recovery, topic polling, images),
versioned settings and prompts, the panel API and HTTP adapter, and the Nuxt
`/superagent` page with six tabs and four-language strings. Gate: a Feishu
mention becomes a Ticket and settings are editable through the panel.

## P4 — Deployment, service script, E2E and docs

Adds the real setup probing, the process entry and composition root, the tmux
service script, hourly-check cron, the four-language wiki and whole-app tests.
Gate: the process boots standalone, survives a restart, and all docs gates pass.

## Status

P-Spike through P4 are implemented. The remaining work is operational: run the
real-repository E2E against a live token and CI, and tune policy rules from the
observed trigger mix.
