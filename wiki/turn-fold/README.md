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

- Completed groups start **collapsed**; the summary names the measured tool
  work (N tools plus the summed durations when known) or the reasoning.
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

The classification is **client-derived on purpose**. Message rows carry
`turn_num`, not a stable turn id, and the trajectory ledger stores tool events
but no assistant text; a server render-unit API or WebSocket frame would have to
duplicate the rows the client already holds. Deriving the unit from the loaded
message protocol keeps history reloads and reconnects identical, changes no
schema and adds no frame.

The optional structured-response summary from the source plan is not used: the
fold works without any LLM cooperation, and the final answer is taken verbatim
from the assistant row.

## 🧪 Tests

`use-chat-turn-fold.test.ts` pins the classification (settled final, streaming,
trailing tool, tool-call-bearing AI, reasoning-only, unknown duration, media-only
and carrier handling). The ChatBox integration suite pins the collapsed summary,
expansion, running-turn linear fallback, ordinary-answer non-fold and the
trailing-tool fail-open. Locale and i18n-block tests cover the four-language
labels.
