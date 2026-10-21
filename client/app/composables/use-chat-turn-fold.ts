/**
 * Collapse classification for one assistant work group (ChatBox).
 *
 * The chat already groups rows by speaker boundaries (`use-chat-turn-groups`):
 * a USER row owns its own group and every following AI/TOOL row is one assistant
 * work group. This module derives the ZCode-style split for such a group:
 *
 *  - `finalMessage` — the group's last AI row, shown even when the work is
 *    collapsed. It must be SETTLED (not streaming), carry a visible body, have
 *    no unresolved tool calls and not be an injected carrier.
 *  - `history` — every row before it (tool cards, intermediate assistant text,
 *    reasoning-only rows), hidden until the reader expands the work.
 *
 * A group without a settled final answer (a running turn, a turn that ended on a
 * tool call, an interrupted turn) is NOT foldable: the caller keeps the existing
 * linear rendering, which is the fail-open rule.
 *
 * The classification is derived from the existing message protocol; it adds no
 * row, column or server frame, so history reloads and reconnects recompute the
 * same unit from the same rows.
 *
 * @module composables/use-chat-turn-fold
 */
import type { MessageItem } from '~/pages/home/type';
import { CHAT_ROLE } from '~/types/chat-role';

export interface TurnFoldView {
  /** Stable view key: the group's first message id. */
  key: number;
  /** Whether the group has a final answer and something worth folding. */
  eligible: boolean;
  /** Rows hidden while collapsed (never contains `finalMessage`). */
  history: MessageItem[];
  /** The settled final assistant row, always visible. */
  finalMessage: MessageItem | null;
  /** Tool cards in the hidden history. */
  toolCount: number;
  /** Sum of the tool rows' measured durations; null when none was reported. */
  durationMs: number | null;
  /** Whether the final answer or any hidden row carries reasoning. */
  hasReasoning: boolean;
}

/**
 * A row that renders as a conversation bubble/tool card (not an injected card).
 * @param message
 */
const isRegularRow = (message: MessageItem): boolean =>
  !(message.origin && message.origin !== 'user' && (message.role === CHAT_ROLE.USER || message.role === CHAT_ROLE.AI));

/**
 * Whether the row has something a bubble can show (text or a media attachment).
 * @param message
 */
const hasVisibleBody = (message: MessageItem): boolean =>
  message.content.trim().length > 0 ||
  (message.images?.length ?? 0) > 0 ||
  (message.audios?.length ?? 0) > 0 ||
  (message.videos?.length ?? 0) > 0;

/**
 * Classify one regular assistant work group.
 *
 * @param group Rows in render order; injected carriers are ignored defensively
 *              (they normally own a singleton group and never reach here).
 * @returns The fold view; `eligible` false means the caller renders linearly.
 */
export function classifyTurnFold(group: MessageItem[]): TurnFoldView {
  const rows = group.filter(isRegularRow);
  const key = rows[0]?.id ?? 0;
  const last = rows.at(-1);
  const empty: TurnFoldView = {
    key,
    eligible: false,
    history: [],
    finalMessage: null,
    toolCount: 0,
    durationMs: null,
    hasReasoning: false
  };
  if (!last) return empty;

  const finalEligible =
    last.role === CHAT_ROLE.AI &&
    !last.streaming &&
    !last.hasToolCalls &&
    (!last.origin || last.origin === 'user') &&
    hasVisibleBody(last);
  if (!finalEligible) return empty;

  const history = rows.slice(0, -1);
  // A tool still running behind the answer (a background terminal, a late
  // tool_end) keeps the work open: hiding a live card would look like it
  // vanished.
  if (history.some(m => m.role === CHAT_ROLE.TOOL && m.toolStatus === 'running')) return empty;

  const hasReasoning = Boolean(last.reasoning?.trim()) || history.some(m => Boolean(m.reasoning?.trim()));
  // A lone answer (no work, no reasoning) has nothing to fold; keeping it
  // linear is what preserves the existing single-bubble rendering.
  if (history.length === 0 && !hasReasoning) return empty;

  const toolRows = history.filter(m => m.role === CHAT_ROLE.TOOL);
  const measured = toolRows
    .map(row => row.toolDurationMs)
    .filter((value): value is number => typeof value === 'number');
  const durationMs = measured.length > 0 ? measured.reduce((total, value) => total + value, 0) : null;
  return {
    key,
    eligible: true,
    history,
    finalMessage: last,
    toolCount: toolRows.length,
    durationMs,
    hasReasoning
  };
}
