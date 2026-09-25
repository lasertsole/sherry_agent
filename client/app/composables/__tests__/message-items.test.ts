import { describe, it, expect } from 'vitest';
import { CHAT_ROLE } from '@/types/chat-role';
import type { MessageItem } from '@/pages/home/type';
import { buildUserInputTokenMap } from '../message-items';

/**
 * Minimal user-message row.
 * @param id
 * @param turnNum
 * @param overrides
 */
const user = (id: string, turnNum: number, overrides: Partial<MessageItem> = {}): MessageItem => ({
  id,
  role: CHAT_ROLE.USER,
  content: 'question',
  turn_num: turnNum,
  timestamp: '',
  ...overrides
});

/**
 * Minimal AI-message row.
 * @param id
 * @param turnNum
 * @param inputTokens
 */
const ai = (id: string, turnNum: number, inputTokens?: number): MessageItem => ({
  id,
  role: CHAT_ROLE.AI,
  content: 'answer',
  turn_num: turnNum,
  timestamp: '',
  ...(inputTokens !== undefined ? { inputTokens } : {})
});

describe('buildUserInputTokenMap', () => {
  it('pairs each AI reply with the user message right before it', () => {
    const rows = [user('u1', 1), ai('a1', 1, 123)];
    const map = buildUserInputTokenMap(rows);
    expect(map.get('u1')).toBe(123);
    expect(map.size).toBe(1);
  });

  it('skips AI messages without a token count', () => {
    const rows = [user('u1', 1), ai('a1', 1)];
    expect(buildUserInputTokenMap(rows).size).toBe(0);
  });

  it('marks only the trailing user bubble of a batch turn', () => {
    const rows = [user('u1', 1), user('u2', 1), ai('a1', 1, 55)];
    const map = buildUserInputTokenMap(rows);
    expect(map.has('u1')).toBe(false);
    expect(map.get('u2')).toBe(55);
  });

  it('skips subagent-completion carrier rows (USER-role system cards)', () => {
    const rows = [user('u1', 1), user('carrier', 1, { origin: 'subagent_completion' }), ai('a1', 1, 77)];
    const map = buildUserInputTokenMap(rows);
    expect(map.has('carrier')).toBe(false);
    expect(map.get('u1')).toBe(77);
  });

  it('pairs normal user rows whose origin is the plain "user" marker', () => {
    const rows = [user('u1', 1, { origin: 'user' }), ai('a1', 1, 10930)];
    const map = buildUserInputTokenMap(rows);
    expect(map.get('u1')).toBe(10930);
  });

  it('does not pair across turns', () => {
    const rows = [user('u1', 1), ai('a1', 2, 42)];
    expect(buildUserInputTokenMap(rows).size).toBe(0);
  });

  it('handles multi-turn conversations', () => {
    const rows = [user('u1', 1), ai('a1', 1, 100), user('u2', 2), ai('a2', 2, 200)];
    const map = buildUserInputTokenMap(rows);
    expect(map.get('u1')).toBe(100);
    expect(map.get('u2')).toBe(200);
    expect(map.size).toBe(2);
  });
});
