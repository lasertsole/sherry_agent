/**
 * Transient in-chat system notices (`system.notice` protocol).
 *
 * The THIRD message kind between the two existing ones: a system-reminder is
 * persisted and reaches the model AND the user (folded card); a dedicated WS
 * panel frame is not persisted and drives a side panel. A system notice is
 * not persisted but DOES land in the chat flow as a transient card — the
 * surface for external tools / CI / internal producers (background command
 * exits, model downloads) whose feedback belongs in the conversation without
 * polluting the history.
 *
 * The list is process-level reactive state: a page refresh loses every notice
 * BY DESIGN (nothing was persisted, so nothing is restored). Cards survive
 * across turns — background commands finish BETWEEN turns, so clearing at a
 * turn boundary would lose them before the user reads them; dismissal is the
 * user's explicit action. Each notice carries its session id: the strip
 * filters by the session it is mounted for, so a KeepAlive-cached page never
 * renders another session's cards.
 */

import { ref } from 'vue';

/** Visual severity the card renders (color + icon). */
export type SystemNoticeLevel = 'info' | 'success' | 'warning' | 'error' | 'progress';

/** One transient card (the frame's `content` payload plus its session + local id). */
export interface SystemNotice {
  id: number;
  sessionId: string;
  title: string;
  content: string;
  level: SystemNoticeLevel;
  source: string;
}

/** The full frame the agent socket emits on `ws:system_notice`. */
export interface SystemNoticeFrame {
  session_id?: unknown;
  content?: {
    title?: unknown;
    content?: unknown;
    level?: unknown;
    source?: unknown;
  } | null;
}

/** The wire payload inside the frame's `content` field. */
export interface SystemNoticeContent {
  title?: unknown;
  content?: unknown;
  level?: unknown;
  source?: unknown;
}

/** Memory bound: the oldest card is dropped when a new one overflows. */
const MAX_NOTICES = 10;

/** Process-scoped transient cards (never persisted, cleared on reload). */
const systemNotices = ref<SystemNotice[]>([]);

let nextId = 1;

/**
 * Normalize one frame into a card; unknown fields degrade to safe defaults
 * instead of throwing (a malformed external push must not break the
 * transcript rendering).
 * @param sessionId The session the frame arrived for (may be empty → dropped by caller).
 * @param content The frame's `content` payload.
 * @returns The normalized card.
 */
function toNotice(sessionId: string, content: SystemNoticeContent): SystemNotice {
  const levels: SystemNoticeLevel[] = ['info', 'success', 'warning', 'error', 'progress'];
  const level = levels.includes(content.level as SystemNoticeLevel) ? (content.level as SystemNoticeLevel) : 'info';
  return {
    id: nextId++,
    sessionId,
    title: String(content.title ?? '').slice(0, 200),
    content: String(content.content ?? ''),
    level,
    source: String(content.source ?? 'external').slice(0, 100)
  };
}

/**
 * Append one notice (called from the WS frame dispatch).
 * @param frame Full frame (`{session_id, content}`); a frame without a session id is dropped.
 * @returns The appended card, or null when the frame carried no session id.
 */
export function pushSystemNotice(frame: SystemNoticeFrame | null | undefined): SystemNotice | null {
  const sessionId = typeof frame?.session_id === 'string' ? frame.session_id : '';
  if (!sessionId || !frame || typeof frame.content !== 'object' || frame.content === null) {
    return null;
  }
  const notice = toNotice(sessionId, frame.content);
  const next = [...systemNotices.value, notice];
  systemNotices.value = next.length > MAX_NOTICES ? next.slice(next.length - MAX_NOTICES) : next;
  return notice;
}

/**
 * Remove one card (the user's dismiss action).
 * @param id
 */
export function dismissSystemNotice(id: number): void {
  systemNotices.value = systemNotices.value.filter(notice => notice.id !== id);
}

/**
 * Drop every card of ONE session (page unmount — notices are per-session state;
 * other sessions' cards stay).
 * @param sessionId
 */
export function clearSessionNotices(sessionId: string): void {
  systemNotices.value = systemNotices.value.filter(notice => notice.sessionId !== sessionId);
}

/** Test + panel seam. */
/**
 * Expose the live list and the three mutations for panels and tests.
 * @returns The notice list plus push / dismiss / clearSession.
 */
export function useSystemNotices() {
  return {
    notices: systemNotices,
    push: pushSystemNotice,
    dismiss: dismissSystemNotice,
    clearSession: clearSessionNotices
  };
}
