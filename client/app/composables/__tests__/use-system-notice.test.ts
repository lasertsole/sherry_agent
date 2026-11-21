import { beforeEach, describe, expect, it } from 'vitest';
import { clearSessionNotices, dismissSystemNotice, pushSystemNotice, useSystemNotices } from '../use-system-notice';

describe('use-system-notice', () => {
  beforeEach(() => {
    clearSessionNotices('');
    // clearSessionNotices('') drops nothing (no notice carries an empty
    // session id); wipe every card directly to isolate each test.
    useSystemNotices().notices.value = [];
  });

  it('push normalizes a frame into a card bound to its session', () => {
    const notice = pushSystemNotice({
      session_id: 's1',
      content: { title: 'bg exited', content: '$ sleep 5', level: 'success', source: 'tool:terminal' }
    });
    expect(notice).toMatchObject({
      sessionId: 's1',
      title: 'bg exited',
      content: '$ sleep 5',
      level: 'success',
      source: 'tool:terminal'
    });
    expect(useSystemNotices().notices.value).toHaveLength(1);
  });

  it('malformed fields degrade to safe defaults instead of throwing', () => {
    const notice = pushSystemNotice({ session_id: 's1', content: { level: 'bogus', title: 42 } });
    expect(notice).toMatchObject({ level: 'info', title: '42', content: '', source: 'external' });
  });

  it('a frame without a session id is dropped', () => {
    expect(pushSystemNotice({ content: { title: 'x' } })).toBeNull();
    expect(pushSystemNotice(null)).toBeNull();
    expect(useSystemNotices().notices.value).toHaveLength(0);
  });

  it('the list is capped and drops the oldest card first', () => {
    for (let i = 0; i < 12; i++) {
      pushSystemNotice({ session_id: 's1', content: { title: `n${i}` } });
    }
    const list = useSystemNotices().notices.value;
    expect(list).toHaveLength(10);
    expect(list[0]!.title).toBe('n2');
    expect(list[9]!.title).toBe('n11');
  });

  it('dismiss removes one card', () => {
    const a = pushSystemNotice({ session_id: 's1', content: { title: 'a' } })!;
    pushSystemNotice({ session_id: 's1', content: { title: 'b' } });
    dismissSystemNotice(a.id);
    expect(useSystemNotices().notices.value.map(n => n.title)).toEqual(['b']);
  });

  it('clearSession drops only that session cards', () => {
    pushSystemNotice({ session_id: 's1', content: { title: 'a' } });
    pushSystemNotice({ session_id: 's2', content: { title: 'b' } });
    clearSessionNotices('s1');
    expect(useSystemNotices().notices.value.map(n => n.sessionId)).toEqual(['s2']);
  });
});
