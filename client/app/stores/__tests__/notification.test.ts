/**
 * The notification store: the state behind the top-bar badge AND the
 * notification tab.
 *
 * Contract: the ws subscription lives here (not in the panel), so the badge
 * keeps counting while the tab is closed; identical consecutive pushes merge
 * into one item with a count; opening the tab is what reads the list; and a push
 * that arrives while the tab is on screen does not re-light the badge.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { setActivePinia } from 'pinia';
import { createTestingPinia } from '@pinia/testing';
import { useNotificationStore } from '../notification';

const mittMocks = vi.hoisted(() => ({ on: vi.fn(), emit: vi.fn(), off: vi.fn() }));

vi.mock('@/composables/mitt', () => mittMocks);

let store: ReturnType<typeof useNotificationStore>;
let handler: ((payload: unknown) => void) | undefined;

/**
 * Push a raw `ws:notification` payload into the captured handler.
 * @param payload
 */
function push(payload: unknown): void {
  handler!(payload);
}

describe('stores/notification', () => {
  beforeEach(() => {
    mittMocks.on.mockClear();
    mittMocks.emit.mockClear();
    setActivePinia(createTestingPinia({ stubActions: false }));
    store = useNotificationStore();
    store.subscribe();
    handler = mittMocks.on.mock.calls.find(call => call[0] === 'ws:notification')?.[1] as
      ((payload: unknown) => void) | undefined;
  });

  it('registers the ws listener exactly once', () => {
    store.subscribe();
    store.subscribe();

    expect(mittMocks.on.mock.calls.filter(call => call[0] === 'ws:notification')).toHaveLength(1);
  });

  it('merges identical consecutive pushes and counts them unread', () => {
    push('heartbeat: 任务完成');
    push('heartbeat: 任务完成');
    push('cron: 每日报告');

    expect(store.items).toHaveLength(2);
    expect(store.items[0]).toMatchObject({ source: 'heartbeat', content: 'heartbeat: 任务完成', count: 2 });
    expect(store.items[1]).toMatchObject({ source: 'cron', count: 1 });
    // Newest first for the panel.
    expect(store.list.map(item => item.content)).toEqual(['cron: 每日报告', 'heartbeat: 任务完成']);
    expect(store.unreadCount).toBe(3);
  });

  it('keeps counting while the panel is closed and stops re-flagging while it is open', () => {
    push('heartbeat: 完成');
    expect(store.unreadCount).toBe(1);
    store.clearAll();

    // Opening the tab reads the list…
    store.setPanelVisible(true);
    push('heartbeat: 完成');
    expect(store.unreadCount).toBe(0);

    // …a second identical push while it stays open still does not light it…
    push('heartbeat: 完成');
    expect(store.unreadCount).toBe(0);
    expect(store.items[0]!.count).toBe(2);

    // …and closing the tab re-arms the badge for the NEXT push, which then shows
    // the TOTAL of the unread pushes (1 + 2), not just the newest one.
    store.setPanelVisible(false);
    push('cron: 新报告');
    expect(store.unreadCount).toBe(3);
  });

  it('zeroes the badge when opened again without dropping the items', () => {
    push('cron: 新报告');
    expect(store.unreadCount).toBe(1);

    store.setPanelVisible(true);
    expect(store.unreadCount).toBe(0);

    // A push while the tab is on screen stays read: it was visible while it arrived.
    push('cron: 新报告');
    store.setPanelVisible(false);
    expect(store.unreadCount).toBe(0);

    // A push after closing flags the whole list again (3 pushes counted), and
    // opening reads it without losing rows.
    push('cron: 新报告');
    expect(store.unreadCount).toBe(3);
    store.setPanelVisible(true);
    expect(store.unreadCount).toBe(0);
    expect(store.items).toHaveLength(1);
    expect(store.items[0]!.count).toBe(3);
  });

  it('clears every item and the badge', () => {
    push('heartbeat: 完成');
    push('cron: 报告');

    store.clearAll();

    expect(store.items).toEqual([]);
    expect(store.list).toEqual([]);
    expect(store.unreadCount).toBe(0);
  });

  it('ignores empty payloads', () => {
    push(null);
    push('');
    push({});

    expect(store.items).toEqual([]);
  });
});
