import { describe, it, expect, beforeEach } from 'vitest';
import { setActivePinia } from 'pinia';
import { createTestingPinia } from '@pinia/testing';
import { useChatFloatStore } from '../chat-float';

describe('stores/chat-float', () => {
  beforeEach(() => {
    setActivePinia(createTestingPinia({ stubActions: false }));
  });

  it('starts docked, centered', () => {
    const store = useChatFloatStore();
    expect(store.floating).toBe(false);
    expect(store.anchor).toBe('center');
    expect(store.width).toBe(0);
    expect(store.height).toBe(0);
    expect(store.collapsed).toBe(false);
  });

  it('stores a resized size and the input-only state', () => {
    const store = useChatFloatStore();
    store.setSize({ width: 640, height: 480 });
    expect(store.width).toBe(640);
    expect(store.height).toBe(480);
    store.setCollapsed(true);
    expect(store.collapsed).toBe(true);
    store.setCollapsed(false);
    expect(store.collapsed).toBe(false);
    // 0 restores the ratio default for that axis.
    store.setSize({ width: 0, height: 0 });
    expect(store.width).toBe(0);
    expect(store.height).toBe(0);
  });

  it('opens, docks and toggles', () => {
    const store = useChatFloatStore();
    store.open();
    expect(store.floating).toBe(true);
    store.dock();
    expect(store.floating).toBe(false);
    store.toggle();
    expect(store.floating).toBe(true);
    store.toggle();
    expect(store.floating).toBe(false);
  });

  it('remembers a valid anchor', () => {
    const store = useChatFloatStore();
    store.setAnchor('bottom-right');
    expect(store.anchor).toBe('bottom-right');
  });

  it('falls back to the default for an unknown anchor', () => {
    const store = useChatFloatStore();
    store.setAnchor('middle' as never);
    expect(store.anchor).toBe('center');
  });
});
