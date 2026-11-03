import { beforeEach, describe, expect, it } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { usePinnedSessionsStore } from '../pinned-sessions';

describe('usePinnedSessionsStore', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it('toggles a session in and out of the pinned set', () => {
    const store = usePinnedSessionsStore();

    store.toggle('s1');
    expect(store.has('s1')).toBe(true);
    expect(store.ids).toEqual(['s1']);

    store.toggle('s1');
    expect(store.has('s1')).toBe(false);
    expect(store.ids).toEqual([]);
  });

  it('keeps pin order while toggling other ids', () => {
    const store = usePinnedSessionsStore();

    store.toggle('s2');
    store.toggle('s1');
    expect(store.ids).toEqual(['s2', 's1']);
  });

  it('forgets a deleted session without touching the rest', () => {
    const store = usePinnedSessionsStore();
    store.toggle('s1');
    store.toggle('s2');

    store.remove('s1');
    expect(store.has('s1')).toBe(false);
    expect(store.has('s2')).toBe(true);
  });

  it('removing an absent id is a no-op', () => {
    const store = usePinnedSessionsStore();
    store.toggle('s1');

    store.remove('missing');
    expect(store.ids).toEqual(['s1']);
  });
});
