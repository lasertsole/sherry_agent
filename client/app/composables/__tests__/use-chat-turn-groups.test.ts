import { describe, it, expect } from 'vitest';
import { ref } from 'vue';
import { useChatTurnGroups } from '../use-chat-turn-groups';
import { CHAT_ROLE } from '@/types/chat-role';
import type { MessageItem } from '@/pages/home/type';

const msg = (over: Partial<MessageItem>): MessageItem => ({
  session_id: 'default',
  role: CHAT_ROLE.USER,
  content: 'x',
  id: 1,
  turn_num: 0,
  timestamp: 't',
  ...over
});

describe('useChatTurnGroups', () => {
  it('drops AI empty placeholders but keeps empty AI rows that carry reasoning', () => {
    const { filteredMessages } = useChatTurnGroups(() => [
      msg({ id: 1, role: CHAT_ROLE.AI, content: '' }),
      msg({ id: 2, role: CHAT_ROLE.AI, content: '', reasoning: 'thinking' }),
      msg({ id: 3, role: CHAT_ROLE.USER, content: '' })
    ]);
    expect(filteredMessages.value.map(m => m.id)).toEqual([2, 3]);
  });

  it('treats a trailing USER message as a turn boundary even when the AI placeholder is filtered', () => {
    // Original sequence: userA, empty AI placeholder, userB -> B is the first message
    // of a new turn, so it must NOT be marked consecutive with userA.
    const { isConsecutive } = useChatTurnGroups(() => [
      msg({ id: 1, role: CHAT_ROLE.USER }),
      msg({ id: 2, role: CHAT_ROLE.AI, content: '' }),
      msg({ id: 3, role: CHAT_ROLE.USER })
    ]);
    expect(isConsecutive(1)).toBe(false);
    expect(isConsecutive(3)).toBe(false);
  });

  it('marks same-role adjacency in the ORIGINAL sequence, skipping TOOL rows', () => {
    const { isConsecutive } = useChatTurnGroups(() => [
      msg({ id: 1, role: CHAT_ROLE.AI }),
      msg({ id: 2, role: CHAT_ROLE.TOOL, content: '' }),
      msg({ id: 3, role: CHAT_ROLE.AI })
    ]);
    // TOOL rows are skipped: ids 1 and 3 are adjacent AI rows.
    expect(isConsecutive(1)).toBe(false);
    expect(isConsecutive(3)).toBe(true);
  });

  it('groups by turn: USER starts a group, consecutive AI/TOOL rows merge', () => {
    const { turnGroups } = useChatTurnGroups(() => [
      msg({ id: 1, role: CHAT_ROLE.USER }),
      msg({ id: 2, role: CHAT_ROLE.AI }),
      msg({ id: 3, role: CHAT_ROLE.TOOL, content: '' }),
      msg({ id: 4, role: CHAT_ROLE.AI }),
      msg({ id: 5, role: CHAT_ROLE.USER })
    ]);
    expect(turnGroups.value.map(g => g.map(m => m.id))).toEqual([[1], [2, 3, 4], [5]]);
  });

  it('turnSpacingClass is true only for multi-row groups not starting with a USER', () => {
    const { turnSpacingClass } = useChatTurnGroups(() => []);
    expect(turnSpacingClass([msg({ id: 1, role: CHAT_ROLE.AI }), msg({ id: 2, role: CHAT_ROLE.AI })])).toBe(true);
    expect(turnSpacingClass([msg({ id: 1, role: CHAT_ROLE.AI })])).toBe(false);
    expect(turnSpacingClass([msg({ id: 1, role: CHAT_ROLE.USER }), msg({ id: 2, role: CHAT_ROLE.AI })])).toBe(false);
  });

  it('splits subagent-completion carriers (USER rows with a non-user origin) out of the bubble flow', () => {
    const { isBackgroundTask, regularMessages, backgroundCarriers } = useChatTurnGroups(() => []);
    const carrier = msg({ id: 1, origin: 'subagent_completion' });
    const legacy = msg({ id: 2, origin: undefined });
    const explicitUser = msg({ id: 3, origin: 'user' });
    const ai = msg({ id: 4, role: CHAT_ROLE.AI });

    expect(isBackgroundTask(carrier)).toBe(true);
    expect(isBackgroundTask(legacy)).toBe(false);
    expect(isBackgroundTask(explicitUser)).toBe(false);
    expect(isBackgroundTask(ai)).toBe(false);

    const group = [carrier, legacy, explicitUser, ai];
    expect(backgroundCarriers(group).map(m => m.id)).toEqual([1]);
    expect(regularMessages(group).map(m => m.id)).toEqual([2, 3, 4]);
  });

  it('reacts to a messages getter backed by a ref', () => {
    const source = ref<MessageItem[]>([msg({ id: 1, role: CHAT_ROLE.USER })]);
    const { turnGroups } = useChatTurnGroups(() => source.value);
    expect(turnGroups.value).toHaveLength(1);
    source.value = [msg({ id: 1, role: CHAT_ROLE.USER }), msg({ id: 2, role: CHAT_ROLE.AI })];
    expect(turnGroups.value.map(g => g.map(m => m.id))).toEqual([[1], [2]]);
  });
});
