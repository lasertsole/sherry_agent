import { describe, it, expect, vi, beforeEach } from 'vitest';
import { effectScope, nextTick, ref } from 'vue';
import { CHAT_ROLE } from '@/types/chat-role';
import type { MessageItem } from '@/pages/home/type';
import { useChatOlderHistory } from '../use-chat-older-history';

const bridge = vi.hoisted(() => ({ get_older_history_page: vi.fn() }));
vi.mock('../messages', () => bridge);

/**
 * Minimal backend row for the page payload.
 * @param id
 * @param turn
 * @param role
 */
const row = (id: number, turn: number, role: CHAT_ROLE = CHAT_ROLE.USER) => ({
  id,
  session_id: 's1',
  role,
  content: `m${id}`,
  turn_num: turn,
  timestamp: 't'
});

const loaded = (turn: number): MessageItem => ({
  id: turn,
  session_id: 's1',
  role: CHAT_ROLE.USER,
  content: 'x',
  turn_num: turn,
  timestamp: 't'
});

type Harness = ReturnType<typeof useChatOlderHistory> & { prepended: MessageItem[][] };

const makeHarness = (sessionId = 's1', initialTurns: number[] = []): Harness => {
  const messages = ref<MessageItem[]>(initialTurns.map(loaded));
  const prepended: MessageItem[][] = [];
  const ctl = useChatOlderHistory({
    sessionId: () => sessionId,
    messages: () => messages.value,
    prepend: rows => prepended.push(rows)
  });
  return { ...ctl, prepended };
};

describe('useChatOlderHistory', () => {
  beforeEach(() => {
    bridge.get_older_history_page.mockReset();
  });

  it('fetches the window below the oldest loaded turn and prepends it', async () => {
    bridge.get_older_history_page.mockResolvedValueOnce({
      rows: [row(1, 1), row(2, 2)],
      exhausted: true
    });
    const h = makeHarness('s1', [11, 12]);
    await h.loadOlder();
    expect(bridge.get_older_history_page).toHaveBeenCalledWith('s1', 11, 12, 10);
    expect(h.prepended[0]!.map(m => m.turn_num)).toEqual([1, 2]);
    expect(h.exhausted.value).toBe(true);
  });

  it('marks exhaustion at turn 1 without requesting', async () => {
    const h = makeHarness('s1', [1, 2]);
    await h.loadOlder();
    expect(bridge.get_older_history_page).not.toHaveBeenCalled();
    expect(h.exhausted.value).toBe(true);
  });

  it('treats an empty page as exhausted', async () => {
    bridge.get_older_history_page.mockResolvedValueOnce({ rows: [], exhausted: true });
    const h = makeHarness('s1', [11, 12]);
    await h.loadOlder();
    expect(h.exhausted.value).toBe(true);
    expect(h.prepended).toHaveLength(0);
  });

  it('a failed request stays retryable (never marked exhausted)', async () => {
    bridge.get_older_history_page.mockResolvedValueOnce(null);
    const h = makeHarness('s1', [11, 12]);
    await h.loadOlder();
    expect(h.exhausted.value).toBe(false);
    expect(h.loadingOlder.value).toBe(false);
  });

  it('guards against concurrent requests', async () => {
    let resolvePage: (v: unknown) => void = () => {};
    bridge.get_older_history_page.mockImplementationOnce(() => new Promise(res => (resolvePage = res)));
    const h = makeHarness('s1', [11, 12]);
    const first = h.loadOlder();
    expect(h.loadingOlder.value).toBe(true);
    await h.loadOlder(); // second call must be a no-op
    expect(bridge.get_older_history_page).toHaveBeenCalledTimes(1);
    resolvePage({ rows: [row(1, 1)], exhausted: false });
    await first;
    expect(h.loadingOlder.value).toBe(false);
  });

  it('marks exhaustion when a page adds nothing older', async () => {
    // Server returned rows that (after filtering) introduce no older turn:
    // prepend receives nothing, oldest stays put -> stop asking.
    bridge.get_older_history_page.mockResolvedValueOnce({ rows: [], exhausted: false });
    const h = makeHarness('s1', [11, 12]);
    await h.loadOlder();
    expect(h.exhausted.value).toBe(true);
  });

  it('keeps a partial page loadable when it is not the start', async () => {
    bridge.get_older_history_page.mockResolvedValueOnce({
      rows: [row(6, 6), row(7, 7)],
      exhausted: false
    });
    const messages = ref<MessageItem[]>([loaded(11)]);
    const prepended: MessageItem[][] = [];
    const h = useChatOlderHistory({
      sessionId: () => 's1',
      messages: () => messages.value,
      prepend: rows => {
        prepended.push(rows);
        messages.value = [...rows, ...messages.value];
      }
    });
    await h.loadOlder();
    expect(h.exhausted.value).toBe(false);
    expect(h.oldestTurn.value).toBe(6);
    await nextTick();
  });

  it('resets state on session switch', async () => {
    const sid = ref('s1');
    const messages = ref<MessageItem[]>([loaded(1)]);
    const scope = effectScope();
    const h = scope.run(() =>
      useChatOlderHistory({
        sessionId: () => sid.value,
        messages: () => messages.value,
        prepend: () => {}
      })
    )!;
    await h.loadOlder();
    expect(h.exhausted.value).toBe(true);
    sid.value = 's2';
    await nextTick();
    expect(h.exhausted.value).toBe(false);
    scope.stop();
  });
});
