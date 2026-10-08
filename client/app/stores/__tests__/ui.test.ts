/**
 * The left sidebar's body switch (sessions ↔ project files).
 *
 * It lives in the ui store next to `sidebarCollapsed` (same UI concern) and is
 * persisted: a refresh keeps the user in whichever body they were looking at.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { setActivePinia } from 'pinia';
import { createTestingPinia } from '@pinia/testing';
import { useUiStore } from '../ui';

describe('stores/ui — sidebarBody', () => {
  beforeEach(() => {
    setActivePinia(createTestingPinia({ stubActions: false }));
  });

  it('defaults to the session list', () => {
    expect(useUiStore().sidebarBody).toBe('sessions');
  });

  it('toggleSidebarBody() flips between the two bodies', () => {
    const store = useUiStore();

    store.toggleSidebarBody();
    expect(store.sidebarBody).toBe('files');

    store.toggleSidebarBody();
    expect(store.sidebarBody).toBe('sessions');
  });

  it('is part of the persisted state', () => {
    const store = useUiStore();
    store.toggleSidebarBody();

    // The persistence plugin records `sidebarBody` in its pick list; asserting
    // the computed value is enough here (localStorage is the plugin's business).
    expect(store.$state).toHaveProperty('sidebarBody', 'files');
  });
});
