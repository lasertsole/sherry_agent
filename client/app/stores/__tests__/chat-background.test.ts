import { describe, it, expect, vi, beforeEach } from 'vitest';
import { ref } from 'vue';
import { setActivePinia } from 'pinia';
import { createTestingPinia } from '@pinia/testing';
import { useChatBackgroundStore } from '../chat-background';

// The store imports `readBackgroundConfig` / `saveBackground` from
// `@/composables/db`; mock the whole module so no Dexie/IndexedDB is touched.
const mocks = vi.hoisted(() => ({
  readBackgroundConfig: vi.fn(),
  saveBackground: vi.fn(async () => undefined)
}));

vi.mock('@/composables/db', () => mocks);

/** setup.ts installs a global `useColorMode` stub; this suite controls its return value. */
function colorModeMock(): ReturnType<typeof vi.fn> {
  return (globalThis as unknown as { useColorMode: ReturnType<typeof vi.fn> }).useColorMode;
}

let store: ReturnType<typeof useChatBackgroundStore>;

describe('stores/chat-background', () => {
  beforeEach(() => {
    mocks.readBackgroundConfig.mockReset();
    mocks.saveBackground.mockReset();
    mocks.saveBackground.mockResolvedValue(undefined);
    colorModeMock().mockReturnValue(ref('dark'));
    setActivePinia(createTestingPinia({ stubActions: false }));
    store = useChatBackgroundStore();
  });

  it('returns the same singleton instance and starts with default state', () => {
    expect(useChatBackgroundStore()).toBe(store);
    expect(store.backgroundUrl).toBe('');
    expect(store.backgroundOpacity).toBe(0);
    expect(store.backgroundLoaded).toBe(false);
  });

  it('chatBackgroundStyle returns undefined without a background url', () => {
    expect(store.chatBackgroundStyle).toBeUndefined();
  });

  it('chatBackgroundStyle produces a cover background-image when a url is set', () => {
    store.backgroundUrl = '/bg/ocean.jpg';
    expect(store.chatBackgroundStyle).toEqual({
      backgroundImage: 'url("/bg/ocean.jpg")',
      backgroundSize: 'cover',
      backgroundPosition: 'center',
      backgroundRepeat: 'no-repeat'
    });
  });

  it('chatBackgroundOverlayStyle maps opacity to color and 0..1 value (dark = black)', () => {
    store.backgroundOpacity = 40;
    expect(store.chatBackgroundOverlayStyle).toEqual({
      backgroundColor: '#000000',
      opacity: 0.4
    });
  });

  it('chatBackgroundOverlayStyle uses white overlay in light mode', () => {
    colorModeMock().mockReturnValue(ref('light'));
    setActivePinia(createTestingPinia({ stubActions: false }));
    store = useChatBackgroundStore();
    store.backgroundOpacity = 100;
    expect(store.chatBackgroundOverlayStyle).toEqual({
      backgroundColor: '#ffffff',
      opacity: 1
    });
  });

  it('loadBackground reads Dexie once (idempotent) and fills the singleton', async () => {
    mocks.readBackgroundConfig.mockResolvedValue({
      backgroundUrl: '/bg/green.jpg',
      backgroundOpacity: 60
    });

    await store.loadBackground();
    expect(store.backgroundUrl).toBe('/bg/green.jpg');
    expect(store.backgroundOpacity).toBe(60);
    expect(store.backgroundLoaded).toBe(true);

    // Second call is a no-op (already loaded).
    mocks.readBackgroundConfig.mockResolvedValue({ backgroundUrl: '/bg/other.jpg', backgroundOpacity: 10 });
    await store.loadBackground();
    expect(store.backgroundUrl).toBe('/bg/green.jpg');
    expect(mocks.readBackgroundConfig).toHaveBeenCalledTimes(1);
  });

  it('loadBackground falls back to empty values when read fails, but marks loaded', async () => {
    mocks.readBackgroundConfig.mockRejectedValue(new Error('boom'));

    await store.loadBackground();
    expect(store.backgroundUrl).toBe('');
    expect(store.backgroundOpacity).toBe(0);
    expect(store.backgroundLoaded).toBe(true);
  });

  it('setBackground updates state and persists to Dexie (survives persistence failure)', async () => {
    await store.setBackground('/bg/red.jpg', 80);
    expect(store.backgroundUrl).toBe('/bg/red.jpg');
    expect(store.backgroundOpacity).toBe(80);
    expect(mocks.saveBackground).toHaveBeenCalledWith('/bg/red.jpg', 80);

    // Persistence failure must not throw / not block the in-memory update.
    mocks.saveBackground.mockRejectedValue(new Error('db down'));
    await expect(store.setBackground('/bg/blue.jpg', 20)).resolves.toBeUndefined();
    expect(store.backgroundUrl).toBe('/bg/blue.jpg');
    expect(store.backgroundOpacity).toBe(20);
  });

  it('setBackground uses current opacity as default when not provided', async () => {
    mocks.saveBackground.mockClear();
    await store.setBackground('/bg/only-url.jpg');
    expect(mocks.saveBackground).toHaveBeenCalledWith('/bg/only-url.jpg', 0);
  });

  it('shares the same singleton state across multiple invocations', () => {
    const other = useChatBackgroundStore();
    other.backgroundUrl = '/bg/shared.jpg';
    expect(store.backgroundUrl).toBe('/bg/shared.jpg');
    expect(store.chatBackgroundStyle?.backgroundImage).toBe('url("/bg/shared.jpg")');
  });
});
