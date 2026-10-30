import { describe, expect, it } from 'vitest';
import { useConversationFind } from '../use-conversation-find';
import type { MessageItem } from '~/pages/home/type';
import { CHAT_ROLE } from '~/pages/home/type';

const message = (id: number, content: string): MessageItem => ({
  session_id: 's',
  role: CHAT_ROLE.USER,
  content,
  id,
  turn_num: 1,
  timestamp: '20261031000000'
});

describe('useConversationFind', () => {
  it('finds every occurrence across messages, in render order', () => {
    const messages = [message(1, 'Error: boom'), message(2, 'no hit'), message(3, 'an ERROR and another error')];
    const find = useConversationFind(() => messages);

    find.search('error');

    expect(find.matches.value).toEqual([
      { messageId: 1, charIndex: 0, length: 5 },
      { messageId: 3, charIndex: 3, length: 5 },
      { messageId: 3, charIndex: 21, length: 5 }
    ]);
    expect(find.total.value).toBe(3);
    expect(find.currentIndex.value).toBe(0);
    expect(find.current.value).toEqual({ messageId: 1, charIndex: 0, length: 5 });
  });

  it('is case-insensitive and keeps the query as typed', () => {
    const find = useConversationFind(() => [message(1, 'HeLLo world')]);

    find.search('hello');

    expect(find.total.value).toBe(1);
    expect(find.query.value).toBe('hello');
  });

  it('clears the results for a blank query', () => {
    const find = useConversationFind(() => [message(1, 'hello')]);

    find.search('   ');

    expect(find.matches.value).toEqual([]);
    expect(find.currentIndex.value).toBe(-1);
    expect(find.current.value).toBeNull();
  });

  it('reports no match for a miss without throwing', () => {
    const find = useConversationFind(() => [message(1, 'hello')]);

    find.search('nope');

    expect(find.total.value).toBe(0);
    expect(find.currentIndex.value).toBe(-1);
    expect(find.current.value).toBeNull();
  });

  it('steps forward and backward, wrapping at both ends', () => {
    const find = useConversationFind(() => [message(1, 'x x x')]);
    find.search('x');
    expect(find.currentIndex.value).toBe(0);

    find.next();
    expect(find.currentIndex.value).toBe(1);
    find.next();
    find.next();
    expect(find.currentIndex.value).toBe(0);

    find.previous();
    expect(find.currentIndex.value).toBe(2);
  });

  it('does nothing when stepping without matches', () => {
    const find = useConversationFind(() => [message(1, 'hello')]);
    find.search('absent');

    find.next();
    find.previous();

    expect(find.currentIndex.value).toBe(-1);
  });

  it('rescans the CURRENT message list (a released row stops matching)', () => {
    let messages = [message(1, 'alpha beta'), message(2, 'beta')];
    const find = useConversationFind(() => messages);
    find.search('beta');
    expect(find.total.value).toBe(2);

    messages = [message(2, 'beta')];
    find.search('beta');

    expect(find.matches.value).toEqual([{ messageId: 2, charIndex: 0, length: 4 }]);
  });

  it('opens and closes the bar, forgetting the query on close', () => {
    const find = useConversationFind(() => [message(1, 'hello')]);

    find.open();
    expect(find.visible.value).toBe(true);
    find.search('hello');
    find.close();

    expect(find.visible.value).toBe(false);
    expect(find.query.value).toBe('');
    expect(find.total.value).toBe(0);
  });

  it('clear() keeps the bar open but drops the matches', () => {
    const find = useConversationFind(() => [message(1, 'hello')]);
    find.open();
    find.search('hello');

    find.clear();

    expect(find.visible.value).toBe(true);
    expect(find.total.value).toBe(0);
  });
});
