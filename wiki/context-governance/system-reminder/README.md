# 🧱 Stable System Prompt & the Reminder Layer

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

> Part of [Context Governance](../README.md): why the system prompt holds only stable content and every live view is injected as a `<system-reminder>` HumanMessage on each turn's first model call.

---

The system prompt holds **stable** content only: persona files, the memory blocks and the skills index. Every LIVE view — plan mode, the working directory, the project instructions (`AGENTS.md`, fallback `CLAUDE.md`), the todo list, the active-work pointer, the TaskFlow board, continuity context — is injected instead as a `<system-reminder source="…">` HumanMessage on each turn's first model call (`DynamicReminderMiddleware` + the catalogue in `agent/middlewares/reminder_catalog/`). The lowest-priority `current_time` source adds the local wall clock to the same block every turn: one English line such as `Current local time: 2026-11-06 09:41:07 (Friday) (CST, UTC+08:00)`. It is `transient=True`, so the previous turn's line is stripped instead of accumulating, and it deliberately does not use `only_if_changed` — the clock must advance. Two reasons for the layer: a prompt whose first tokens change per turn invalidates the provider's prefix cache for everything after it (persona included), and a prompt that is only rebuilt on a cache miss shows a STALE todo list between rebuilds.

The frozen memory snapshot has one deliberate exception. A session model switch starts a new provider cache namespace, and a thinking switch was measured to drop `cache_read` to 0 on an identical prefix; the cache is already cold either way. `ThinkingControlMiddleware`'s `before_model` hook compares the turn's `(thinking key, profile fingerprint, mapping fingerprint)` with the last applied one (process-level, because the graph is rebuilt every turn) and, on a change, reloads `MemoryStore`'s snapshot and deletes `StateKey.SYSTEM_PROMPT` from both registers before `system_prompt_injection` runs. The first model call of the switched turn therefore sees mid-session memory-entry writes (the store is SQLite-backed; the legacy Markdown files are only imported once). A first turn, an unchanged control, and a failed reload all keep the cached prompt; no turn is triggered without a send.

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

The catalogue registers nine sources (`agent/middlewares/reminder_catalog/sources.py::register_default_sources`). All ride the same channel (`CURRENT_TURN`); most carry `only_if_changed=True` — a source whose payload is byte-identical to its last injection is **not** re-injected, even when its cooldown has elapsed. `current_time` deliberately does not (the clock must advance every turn), and `memory_recall` re-derives its hits from the current request. The catalogue resolves **once per turn, on the first model call only** (when the last message is a genuine user message); a resumed turn that already carries the block before its user message skips re-injection.

| Source (`source_id`) | Priority | Budget (chars) | Cooldown | Transient | Injects |
|---|---|---|---|---|---|
| `memory_recall` | 88 | 4 000 | 3 turns | No | Semantic memory hits relevant to the current request (only while the switch is on) |
| `plan_mode_block` | 85 | 2 000 | 0 — every turn | No | Plan-mode status / directive (whether the agent is in plan mode) |
| `todo_block` | 80 | 1 500 | 0 — every turn | No | The agent's current todo / working plan list |
| `taskflow_status` | 75 | 2 000 | 5 turns | No | TaskFlow board state (steered board) |
| `boulder_status` | 70 | 1 000 | 5 turns | No | Boulder / active-work pointer |
| `continuity_context` | 65 | 1 000 | 0 — every turn | No | Carry-over continuity context across turns |
| `project_instructions` | 62 | 8 000 | 0 — every turn | Yes | The project's `AGENTS.md` (fallback `CLAUDE.md`) |
| `project_dir_notice` | 60 | 500 | 0 — every turn | No | Working directory notice |
| `current_time` | 10 | 120 | 0 — every turn | Yes | Local wall clock in English: date, weekday, seconds, UTC offset |

**How to read the frequency.** `cooldown_turns = 0` means *eligible every turn* — the source re-injects whenever its content changed since the last injection; a source whose content rarely changes (like `project_instructions`) injects rarely despite being eligible every turn, while one that changes often (like `todo_block`) injects most turns. `current_time` is the exception: its content carries seconds and `only_if_changed` is off, so it is present every turn. `cooldown_turns = 5` (TaskFlow board, boulder) means *at most once every 5 turns*, and only then if the content also moved; a board that did not change is silent for 5 turns even if it would otherwise be eligible. The throttle bookkeeping advances only for sources actually injected, so a suppressed source does not "consume" its cooldown.

**Compression filtering.** Each source declares `compress_exclude` (default `True`); `DynamicReminderMiddleware` stamps it into the message metadata and `_filter_summary_messages` drops those reminders before the summary prompt is built. The live views are re-derived every turn, so a pre-compression snapshot is noise; `continuity_context` is the only source that sets `compress_exclude=False` and is serialized as `[Continuity]`. Reminders injected before the tag existed carry no key and keep the legacy behavior. Real user turns are identified by `pub.func.message.classify.is_user_message` (`metadata.origin == "user"`, fail-closed), so an injected HumanMessage is never labelled `[User]` in a summary.

**Persistence across turns.** Non-transient reminders are `HumanMessage`s in the transcript; the middleware's whole-list rewrite re-emits them through `messages[:-1]` on subsequent turns, so they stay in the context window until a compaction discards them (the post-compaction `reset_session` clears the change-detection hash, so the next turn re-injects from live state). **Transient sources** (`transient=True`) are the exception: the rewrite strips their old reminder from `messages[:-1]` on every turn they are not re-resolved. `project_instructions` therefore appears only on the turn its content changed (and the first turn after compaction); `current_time` is re-resolved every turn, and stripping its previous line keeps exactly ONE clock line in context instead of one per turn.

**Injection gate chain.** A source must pass every check below, in order, to be injected on a given turn (`resolve_injections` in `core.py` + the middleware gate in `dynamic.py`):

1. **Catalog enabled** — `REMINDER_CATALOG["enabled"]` is `True`; if off, nothing injects.
2. **First model call of the turn** — the middleware fires only when the last message is a genuine (non-injected) `HumanMessage`; a mid-turn tool-loop call and a resumed turn that already carries the block are skipped (`_already_injected_this_turn`).
3. **Cooldown elapsed** — if `cooldown_turns > 0` and the source was previously injected, at least N turns must have passed since; a never-injected source passes immediately (not cooling down from turn 0).
4. **Builder returns content** — the builder runs; `None`, blank, or an exception skips the source (fail-open — one broken source never kills the layer).
5. **Content changed** — if `only_if_changed=True`, the content's SHA-256 hash is compared to the last injected hash; identical content is skipped.
6. **Total budget** — surviving candidates are sorted by priority descending; the lowest-priority source is dropped whole until the remaining set fits under `REMINDER_CATALOG["total_budget_chars"]` (30 000). A dropped source is NOT marked as shown, so it remains eligible next turn.

**Priority is the budget order.** `resolve_injections` sorts by descending priority; under the `REMINDER_CATALOG["total_budget_chars"]` (30 000) ceiling the lowest-priority source is dropped whole (never truncated mid-thought) until the remaining set fits. Delivery reverses the kept set so the highest-priority view (plan mode, then the todo list) lands at the tail of the block, immediately before the turn's user message — live state first, then the request.

**Tool-result enrichment** is a separate, per-call queue: it appends advisories to a `ToolMessage` after the tool returns (JSON tools get an `advisory` field, plain text gets a suffix). It is orthogonal to this turn-level reminder layer; see [Tool-Result Enrichment](../tool-result-enrichment/README.md).

**Not part of this catalogue.** `TaskIntent` steering, the todo-continuation directive, and workspace notices keep their own injection paths and are not registered here; the catalogue is an overlay, migration is batched.
**DelegationPressureGate** (`agent/middlewares/delegation_pressure/core.py`) injects its warn / escalated directives as `<system-reminder source="delegation_pressure">` messages with the standard reminder metadata contract (`provenance="reminder_catalog"`), through its OWN `abefore_model` path — not the catalogue's `resolve_injections`. The catalogue resolves only on the turn's first model call (when the last message is a genuine user message); delegation pressure triggers mid-turn (after 7 / 14 tool calls, on subsequent model calls), so the first-call-only gate would block it. It is conceptually priority 100: on the mid-turn calls where it fires the catalogue is a no-op, and the directive is appended at the tail of the message list, closest to the generation point. Tools remain available; it warns, it never blocks.
