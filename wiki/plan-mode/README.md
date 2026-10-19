# Plan-Then-Execute Plan Mode

**English** · [中文](README.zh.md) · [日本語](README.ja.md) · [한국어](README.ko.md)

A session can design before it executes: the agent enters **plan mode**, works
with read-only tools, submits a plan, and waits for the user's approval before a
single file is written. The alternative — running straight at a multi-file
change — is where the agent drifts, and the user gets no chance to redirect it.

Source of truth: `agent/tools/plan_mode/` (the two tools + the shared state
helpers), `agent/middlewares/tool_selection/core.py` (the whitelist),
`workspace/prompt_builder.py::_build_plan_mode_block` (the reminder),
`config/features/agent_side/plan_mode.py` (the tunables).

## The loop

```
                  enter_plan_mode
   needs_planning ────────────────► planning ──────────────► ready
        ▲                              │  ▲                   │
        │        reject (no message)   │  │ reject (message)  │ enter_plan_mode(replan=True)
        └──────────────────────────────┘  └─── revise ────────┘
                                                               │
                                            needs_replan ◄─────┘
                                                  │  (exit_plan_mode + approval)
                                                  └──────────► ready
```

1. `enter_plan_mode(reason)` — the agent decides a task is worth a plan
   (multi-file change, architectural decision, unclear requirements, several
   valid approaches). The session's plan status becomes `planning` and the tool
   face narrows to the read-only whitelist.
2. The agent explores (`read_file`, `search_files`, `message_search`, the skill
   viewer), asks the user with `question` when something is ambiguous, and
   designs. A turn in plan mode may only end with a `question` or an
   `exit_plan_mode` call.
3. `exit_plan_mode(plan)` — persists the plan under
   `workspace/sessions/<session_id>/plans/plan-<revision>.md` and suspends the
   graph for approval (the chat shows the plan with **Approve** and **Request
   changes**).
4. **Approve** → status `ready`; the plan stays the session's reference (the
   system prompt names the file every turn), and the write tools are back.
   **Request changes** (reject + feedback) → the feedback is returned to the
   agent, which revises and resubmits. **Reject** (no message) → the plan is
   dropped and the agent decides whether to re-plan.

## The read-only guarantee

While the status is `planning` / `needs_replan`, `ToolSelectionMiddleware`
applies the whitelist twice, exactly like the per-session tool selection it
already did: the model is only OFFERED the whitelist, and a call that still
arrives (a stale checkpoint, a hallucinated name) is REFUSED at execution with
"Plan mode is active". The default whitelist is
`config/features/agent_side/plan_mode.py::PLAN_MODE["allowed_tools"]`; it is
still bounded by the session's own tool selection, except that the two plan
tools always stay reachable — a mode the model cannot leave would be a trap.

Subagents are the one deliberate hole closed: `sessions_spawn` is NOT on the
whitelist, because a child runs with its own session's state and a spawned
executor could write before the plan is approved.

## Guards

* **Mutation budget** — `PLAN_MODE["max_mutations"]` (5) reject-with-feedback
  rounds per plan. Past it, `exit_plan_mode` refuses to resubmit and tells the
  agent to ask the user for a plain decision.
* **Revision binding** — the approval binds to `sha256(plan)[:16]`. If the
  stored revision or the plan FILE changes while the approval is pending, the
  approval is discarded and the plan must be resubmitted.
* **Size cap** — `PLAN_MODE["max_plan_chars"]` (20000); a longer plan is
  refused with a "tighten it" message rather than truncated.
* **Disabled feature** — `PLAN_MODE["enabled"] = false` unregisters both tools,
  and nothing else changes.

## What the user sees

The plan arrives as an approval card in the input slot (the same surface HITL
already uses): the plan text, a feedback field, **Request changes**, and
**Approve**. Approving resumes the turn with execution tools available; asking
for changes resumes it with the feedback and the agent revises the plan.

## Relationship to the other gates

Plan mode is approval BEFORE execution; HITL is approval DURING execution (and
still applies to the operations it always did once execution starts). TaskFlow
is execution AFTER approval: the plan's numbered steps can be registered as
flow steps or todos, and the completion gates judge the result.
