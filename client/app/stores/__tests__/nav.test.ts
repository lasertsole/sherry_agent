/**
 * Left-sidebar navigation registry (UI upgrade S1). Replaces the old
 * `sidebarBody` binary: items are extensible, activate is validated and the
 * active id is persisted.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { setActivePinia } from 'pinia';
import { createTestingPinia } from '@pinia/testing';
import { defineComponent } from 'vue';
import { useNavStore } from '../nav';

const Panel = defineComponent({ template: '<div />' });

describe('stores/nav — left sidebar registry', () => {
  beforeEach(() => {
    setActivePinia(createTestingPinia({ stubActions: false }));
  });

  it('registers the two built-in items and defaults to chat', () => {
    const store = useNavStore();
    expect(store.items.map(i => i.id)).toEqual(['chat', 'files', 'experts', 'tasks', 'meetings']);
    expect(store.activeId).toBe('chat');
    expect(store.activeItem?.id).toBe('chat');
  });

  it('activates a registered item and ignores unknown ids', () => {
    const store = useNavStore();
    store.activate('files');
    expect(store.activeId).toBe('files');
    store.activate('does-not-exist');
    expect(store.activeId).toBe('files');
  });

  it('registers a new item idempotently and can unregister it', () => {
    const store = useNavStore();
    const item = {
      id: 'experts',
      icon: 'pi-users',
      labelKey: 'nav.experts',
      component: Panel,
      scope: 'global' as const
    };
    store.register(item);
    store.register(item);
    expect(store.items.filter(i => i.id === 'experts')).toHaveLength(1);
    store.activate('experts');
    expect(store.activeId).toBe('experts');
    store.unregister('experts');
    expect(store.items.some(i => i.id === 'experts')).toBe(false);
    expect(store.activeId).toBe('chat');
  });

  it('exposes the active id in persisted state', () => {
    const store = useNavStore();
    store.activate('files');
    expect(store.$state).toHaveProperty('activeId', 'files');
  });
});
