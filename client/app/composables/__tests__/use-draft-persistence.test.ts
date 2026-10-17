import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { ref } from 'vue';
import { CHAT_ROLE } from '@/types/chat-role';
import type { MessageItem } from '@/pages/home/type';
import { useDraftPersistence } from '../use-draft-persistence';

const db = vi.hoisted(() => ({
  saveDraftTurn: vi.fn(async () => {}),
  clearDraftTurn: vi.fn(async () => {})
}));
vi.mock('../db', () => db);

/**
 * Minimal in-flight turn row (only the fields the draft slice reads).
 * @param id
 * @param turn
 */
const row = (id: number, turn: number) => ({
  id,
  session_id: 's1',
  role: CHAT_ROLE.USER,
  content: `m${id}`,
  turn_num: turn,
  timestamp: 't'
});

describe('useDraftPersistence', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('the 200ms text-append debounce writes one merged snapshot', async () => {
    const messages = ref([row(1, 1)] as unknown as MessageItem[]);
    const drafts = useDraftPersistence(messages);

    drafts.scheduleDraftWrite('s1', 1);
    drafts.scheduleDraftWrite('s1', 1); // repeated append restarts the timer
    await vi.advanceTimersByTimeAsync(200);

    expect(db.saveDraftTurn).toHaveBeenCalledTimes(1);
    const draft = db.saveDraftTurn.mock.calls[0]![0] as {
      session_id: string;
      turn_num: number;
      messages: MessageItem[];
    };
    expect(draft.session_id).toBe('s1');
    expect(draft.turn_num).toBe(1);
    expect(draft.messages).toHaveLength(1);
  });

  it('dispose cancels the pending text-append debounce (nothing fires after unmount)', async () => {
    const messages = ref([row(1, 1)] as unknown as MessageItem[]);
    const drafts = useDraftPersistence(messages);

    drafts.scheduleDraftWrite('s1', 1);
    drafts.dispose();
    await vi.advanceTimersByTimeAsync(1000);

    expect(db.saveDraftTurn).not.toHaveBeenCalled();
  });

  it('dispose clears the active-turn registry', () => {
    const messages = ref([row(1, 2)] as unknown as MessageItem[]);
    const drafts = useDraftPersistence(messages);

    drafts.trackDraftTurn('s1', 2);
    expect(drafts.isDraftTurnActive(2)).toBe(true);

    drafts.dispose();
    expect(drafts.isDraftTurnActive(2)).toBe(false);
    expect(drafts.activeTurns()).toEqual([]);
  });

  it('commitDraftTurn flushes immediately and cancels the pending debounce', async () => {
    const messages = ref([row(1, 3)] as unknown as MessageItem[]);
    const drafts = useDraftPersistence(messages);

    drafts.scheduleDraftWrite('s1', 3);
    await drafts.commitDraftTurn('s1', 3);
    await vi.advanceTimersByTimeAsync(500);

    // Immediate write only — the cancelled debounce never adds a second one.
    expect(db.saveDraftTurn).toHaveBeenCalledTimes(1);
  });
});
