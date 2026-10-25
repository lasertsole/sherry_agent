# Turn History Folding

> A settled turn collapses its tool/reasoning history behind one summary while
> the final assistant answer stays visible.

## 🎯 Overview

After a generation turn settles, the chat folds the assistant's execution
history — tool cards, intermediate assistant text and reasoning — behind a small
summary row. The final assistant answer is always shown below it. Expanding the
summary restores the full linear history.

A turn that is still running, ended on a tool call, has no final assistant answer
or carries unresolved tool calls is **not** folded: it keeps the existing linear
rendering. This is the fail-open boundary — an abnormal turn never hides work.

## 🧭 Classification

The fold is derived from the existing message rows. An assistant work group is
foldable only when all of these hold:

| Condition | Requirement |
| :--- | :--- |
| Final row | The group's last row is an AI row |
| Settled | `streaming` is false |
| Visible | Content or a media attachment is present |
| Complete | The row declares no tool calls (`hasToolCalls` false) |
| Regular | It is not an injected carrier (no non-user `origin`) |
| Something to fold | It has history rows before it, or reasoning |

`history` is every row before the final answer; a lone answer with no reasoning
has nothing to fold and stays linear.

## ⚙️ Interaction

- Completed groups start **collapsed**; the summary shows the tool-card count and
  the summed duration of measured tool calls. This is **tool execution time**, not
  the turn's wall-clock (model thinking and network time are excluded).
  Without tools it shows the reasoning/work label.
- The summary row toggles the history in place; the per-tool cards inside keep
  their own expand/collapse state.
- While collapsed, the final row renders without its thinking block and keeps
  its avatar, so it is not mistaken for a consecutive continuation.
- The expansion state is per mounted chat page (not persisted); reloading
  recomputes the same classification and starts collapsed again.
- The virtual list re-measures the row after a toggle, so scroll geometry stays
  correct.

## 🧩 Architecture

`use-chat-turn-groups` already splits rows by speaker boundaries: a USER row owns
its own group and the following AI/TOOL rows form one assistant work group.
`use-chat-turn-fold.ts::classifyTurnFold` turns that group into
`{history, finalMessage, toolCount, durationMs, hasReasoning}`.

`MessageItem.hasToolCalls` is mapped from the stored AI row's `tool_calls`, so a
tool-call-bearing row can never be mistaken for the final answer.
`ChatTurnHistoryStatus.vue` is the summary trigger; `ChatBox.vue` chooses the
visible rows and suppresses the final row's reasoning while collapsed;
`use-chat-card-expansion.ts` owns the per-group expansion set.

The classification is **client-derived on purpose**. Message rows now carry both
`turn_num` and `turn_id` (stamped by the server per turn), but the trajectory
ledger stores tool events and no assistant text; a server render-unit API or
WebSocket frame would duplicate the rows the client already holds. Deriving the
unit from the loaded message protocol keeps history reloads and reconnects
identical, changes no schema and adds no frame.

The final answer is ordinary `AIMessage.content` — there is no `final_answer` /
`execution_summary` / `structured_response` field; "settled" is decided by the
row's empty `tool_calls`. The optional structured-response summary from the
source plan is not used: the fold needs no LLM cooperation.

## ⚠️ Spike & known limitation (2026-10-23)

A real session ran two `read_file` calls plus one `search_files`, with thinking on:

- while the turn was running: 0 fold triggers, tool cards visible (linear);
- settled after reload: summary "3 tools · 54ms", tool cards/reasoning hidden,
  final answer visible;
- expanded: 3 tool cards + 2 thinking blocks restored, final answer still visible;
- 54ms = 9 + 8 + 37ms, confirming the summary is the sum of tool durations.

**Known live-session race (not fixed yet)**: immediately after a turn ends, before a
reload, draft rows can be hydrated back and sorted before the new user row. This
duplicates the final answer and inflates/misgroups the fold summary (measured
before reload: "4 tools · 62ms"; after reload: "3 tools · 54ms"). Root cause:
`untrackDraftTurn` starts the IndexedDB clear without awaiting it, then
`loadSessionHistory` runs while the server may not have persisted the final AI row
yet, so the stale-draft guard cannot recognize it. Reloading restores the correct
view. Fix direction: await the draft clear before loading history, or skip draft
hydration for a turn that already has a human row but no final AI row.

Verdict: the fold classification is sound and needs neither a server render-unit
protocol nor a structured-response field; the race belongs to the existing
chat/draft layer and needs a separate fix.

## 🧪 Tests

`use-chat-turn-fold.test.ts` pins the classification (settled final, streaming,
trailing tool, tool-call-bearing AI, reasoning-only, unknown duration, media-only
and carrier handling). The ChatBox integration suite pins the collapsed summary,
expansion, running-turn linear fallback, ordinary-answer non-fold and the
trailing-tool fail-open. Locale and i18n-block tests cover the four-language
labels.
