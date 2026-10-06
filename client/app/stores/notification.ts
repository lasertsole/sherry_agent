import { defineStore } from 'pinia';
import dayjs from 'dayjs';

/**
 * Notification state for the heartbeat / cron completion pushes.
 *
 * The state lives in a STORE, not in the panel that renders it: the unread badge
 * in the top bar must keep counting while the notification TAB is closed (the
 * subscription used to sit in the permanently mounted dialog for the same
 * reason). `subscribe()` registers the mitt listener once; the shell calls it on
 * mount, so a push arriving with no tab open still updates the badge.
 */

/** One notification (identical consecutive pushes merge into a count). */
export interface NotificationItem {
  source: 'heartbeat' | 'cron';
  content: string;
  count: number;
  time: string;
}

/**
 * Notification content (a WS-pushed payload may be an object or a string).
 * @param payload Raw `ws:notification` payload.
 * @returns The display text, `''` when there is nothing to show.
 */
function contentOf(payload: unknown): string {
  if (payload == null) return '';
  if (typeof payload === 'string') return payload.trim();
  try {
    const text = JSON.stringify(payload);
    return text && text !== '{}' ? text : '';
  } catch {
    return '';
  }
}

/**
 * Determine the notification source (the server prefixes a marker; a payload
 * without one falls back to content keywords).
 * @param content Notification text.
 * @returns Which produced it.
 */
function sourceOf(content: string): NotificationItem['source'] {
  if (/^(heartbeat|cron):\s*/.test(content)) return content.startsWith('heartbeat:') ? 'heartbeat' : 'cron';
  return /\b(cron|定时|定時)\b/i.test(content) ? 'cron' : 'heartbeat';
}

export const useNotificationStore = defineStore('notification', () => {
  /** All notifications, oldest first (the UI reverses for display). */
  const items = ref<NotificationItem[]>([]);

  /** Whether the current list counts as read (the badge shows 0 then). */
  const read = ref(false);

  /** True while the notification TAB is the active right-sidebar tab. */
  const panelVisible = ref(false);

  /** Singleton guard: the mitt listener is registered exactly once. */
  const subscribed = ref(false);

  /** Newest-first view for the panel. */
  const list = computed<NotificationItem[]>(() => [...items.value].reverse());

  /** Badge count: the total of unread pushes, 0 once read. */
  const unreadCount = computed<number>(() => (read.value ? 0 : items.value.reduce((sum, item) => sum + item.count, 0)));

  /**
   * Merge one pushed notification: an identical push repeats the newest item
   * (count++), anything else appends. A push while the panel is closed marks the
   * list unread again, so the badge lights up.
   * @param payload Raw `ws:notification` payload.
   */
  function handleNotification(payload: unknown): void {
    const content = contentOf(payload);
    if (!content) return;
    const latest = items.value.length > 0 ? items.value[items.value.length - 1] : null;
    if (latest && latest.content === content) {
      latest.count += 1;
    } else {
      items.value.push({ source: sourceOf(content), content, count: 1, time: dayjs().format('HH:mm:ss') });
    }
    if (!panelVisible.value) read.value = false;
  }

  /** Zero the badge (opening the tab counts as reading; the items are kept). */
  function markRead(): void {
    read.value = true;
  }

  /** Drop every notification. */
  function clearAll(): void {
    items.value = [];
    read.value = true;
  }

  /** Register the WS push listener once (called by the shell on mount). */
  function subscribe(): void {
    if (subscribed.value) return;
    subscribed.value = true;
    on('ws:notification', handleNotification);
  }

  /**
   * The notification tab was mounted / unmounted: while it is visible a push does
   * not re-flag the list as unread, and mounting counts as reading it.
   * @param visible Whether the panel is on screen.
   */
  function setPanelVisible(visible: boolean): void {
    panelVisible.value = visible;
    if (visible) markRead();
  }

  return {
    items,
    list,
    unreadCount,
    subscribed,
    panelVisible,
    handleNotification,
    markRead,
    clearAll,
    subscribe,
    setPanelVisible
  };
});
