# 🧱 Stable System Prompt & the Reminder Layer

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

> Part of [Context Governance](../README.md): why the system prompt holds only stable content and every live view is injected as a `<system-reminder>` HumanMessage on each turn's first model call.

---

The system prompt holds **stable** content only: persona files, the memory blocks and the skills index. Every LIVE view — plan mode, the working directory, the project instructions (`AGENTS.md`, fallback `CLAUDE.md`), the todo list, the active-work pointer, the TaskFlow board, continuity context, the knowledge index — is injected instead as a `<system-reminder source="…">` HumanMessage on each turn's first model call (`DynamicReminderMiddleware` + the catalogue in `agent/middlewares/reminder_catalog/`). Two reasons: a prompt whose first tokens change per turn invalidates the provider's prefix cache for everything after it (persona included), and a prompt that is only rebuilt on a cache miss shows a STALE todo list between rebuilds.

The frozen memory snapshot has one deliberate exception. A session model switch starts a new provider cache namespace, and a thinking switch was measured to drop `cache_read` to 0 on an identical prefix; the cache is already cold either way. `ThinkingControlMiddleware`'s `before_model` hook compares the turn's `(thinking key, profile fingerprint, mapping fingerprint)` with the last applied one (process-level, because the graph is rebuilt every turn) and, on a change, reloads `MemoryStore`'s snapshot and deletes `StateKey.SYSTEM_PROMPT` from both registers before `system_prompt_injection` runs. The first model call of the switched turn therefore sees mid-session `FACTS.md` / `MEMORY.md` / `USER.md` writes. A first turn, an unchanged control, and a failed reload all keep the cached prompt; no turn is triggered without a send.

FACTS.md is global, so a directory switch no longer invalidates the cached prompt for memory. The same turn's `project_instructions` reminder still re-reads the new project's `AGENTS.md` (fallback `CLAUDE.md`) and injects it once per distinct content (`only_if_changed`).

| Concern | Rule |
|---|---|
| What is injected | One reminder per source whose builder returned text: budget order high→low, delivered low→high so the highest priority sits at the tail, tagged `origin=<source id>`, `provenance="reminder_catalog"`, `internal=True` |
| Budget | `REMINDER_CATALOG["total_budget_chars"]` (30 000) across all sources per call — the lowest priority is dropped whole, never truncated mid-thought |
| Throttle | A source may declare `cooldown_turns` (TaskFlow board / boulder: 5) and `only_if_changed` (an identical payload is not repeated); the bookkeeping advances only for sources actually injected |
| After compression | The compaction resets the catalogue, so the next turn re-injects every source from live state — the reminder layer does not depend on history surviving |
| Escaping | Nested `<system-reminder>` tags inside a payload (todo text is user-influenced) are escaped, or the payload could close the tag and impersonate the layer |
| Turn scoping | Injected on the first model call of a turn only — the same "last message is a genuine user message" test TaskIntent uses; the scanners that walk human messages (`split_into_turns`, HITL's headless check, the summarizer labels) skip reminders through `pub.func.message.system_reminder` |

The todo list has one extra guard: `PlanContinuationEnforcer` (renamed from `TodoContinuationEnforcer`; it supervises the plan = todos + TaskFlow flows) counts turns without a `todowrite` call and, past `TODOLIST_INFRA["todo_write_nudge_turns"]` (10), appends a `[REMINDER: TODO TRACKING]` nudge to the continuation directive — the one shape the snapshot-based stagnation check cannot catch (an agent advancing TaskFlow clears the streak while the todo list rots).

## 📋 Registered Sources

The catalogue registers eight sources (`agent/middlewares/reminder_catalog/sources.py::register_default_sources`). All ride the same channel (`CURRENT_TURN`) and all carry `only_if_changed=True` — a source whose payload is byte-identical to its last injection is **not** re-injected, even when its cooldown has elapsed. The catalogue resolves **once per turn, on the first model call only** (when the last message is a genuine user message); a resumed turn that already carries the block before its user message skips re-injection.

| Source (`source_id`) | Priority | Budget (chars) | Cooldown | Injects |
|---|---|---|---|---|
| `plan_mode_block` | 85 | 2 000 | 0 — every turn | Plan-mode status / directive (whether the agent is in plan mode) |
| `todo_block` | 80 | 1 500 | 0 — every turn | The agent's current todo / working plan list |
| `taskflow_status` | 75 | 2 000 | 5 turns | TaskFlow board state (steered board) |
| `boulder_status` | 70 | 1 000 | 5 turns | Boulder / active-work pointer |
| `continuity_context` | 65 | 1 000 | 0 — every turn | Carry-over continuity context across turns |
| `project_instructions` | 62 | 8 000 | 0 — every turn | The project's `AGENTS.md` (fallback `CLAUDE.md`) |
| `project_dir_notice` | 60 | 500 | 0 — every turn | Working directory notice |
| `knowledge_index` | 50 | 1 000 | 0 — every turn | Knowledge index |

**How to read the frequency.** `cooldown_turns = 0` means *eligible every turn* — the source re-injects whenever its content changed since the last injection; a source whose content rarely changes (like `project_instructions`) injects rarely despite being eligible every turn, while one that changes often (like `todo_block`) injects most turns. `cooldown_turns = 5` (TaskFlow board, boulder) means *at most once every 5 turns*, and only then if the content also moved; a board that did not change is silent for 5 turns even if it would otherwise be eligible. The throttle bookkeeping advances only for sources actually injected, so a suppressed source does not "consume" its cooldown.

**Persistence across turns.** An injected reminder is a `HumanMessage` in the transcript; the middleware's whole-list rewrite re-emits it through `messages[:-1]` on subsequent turns, so it stays in the context window until a compaction discards it (the post-compaction `reset_session` clears the change-detection hash, so the next turn re-injects from live state). This means a large `only_if_changed` source like `project_instructions` (≤ 8 000 chars) occupies the message list every turn after its first injection — even on turns it is not re-resolved.

**Injection gate chain.** A source must pass every check below, in order, to be injected on a given turn (`resolve_injections` in `core.py` + the middleware gate in `dynamic.py`):

1. **Catalog enabled** — `REMINDER_CATALOG["enabled"]` is `True`; if off, nothing injects.
2. **First model call of the turn** — the middleware fires only when the last message is a genuine (non-injected) `HumanMessage`; a mid-turn tool-loop call and a resumed turn that already carries the block are skipped (`_already_injected_this_turn`).
3. **Cooldown elapsed** — if `cooldown_turns > 0` and the source was previously injected, at least N turns must have passed since; a never-injected source passes immediately (not cooling down from turn 0).
4. **Builder returns content** — the builder runs; `None`, blank, or an exception skips the source (fail-open — one broken source never kills the layer).
5. **Content changed** — if `only_if_changed=True`, the content's SHA-256 hash is compared to the last injected hash; identical content is skipped.
6. **Total budget** — surviving candidates are sorted by priority descending; the lowest-priority source is dropped whole until the remaining set fits under `REMINDER_CATALOG["total_budget_chars"]` (30 000). A dropped source is NOT marked as shown, so it remains eligible next turn.

**Priority is the budget order.** `resolve_injections` sorts by descending priority; under the `REMINDER_CATALOG["total_budget_chars"]` (30 000) ceiling the lowest-priority source is dropped whole (never truncated mid-thought) until the remaining set fits. Delivery reverses the kept set so the highest-priority view (plan mode, then the todo list) lands at the tail of the block, immediately before the turn's user message — live state first, then the request.

**Not part of this catalogue.** `TaskIntent` steering, the todo-continuation directive, and workspace notices keep their own injection paths and are not registered here; the catalogue is an overlay, migration is batched.
