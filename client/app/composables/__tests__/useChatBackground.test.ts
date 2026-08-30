import { describe, it, expect, vi, beforeEach, beforeAll } from 'vitest';
import { ref, computed } from 'vue';

// `useChatBackground.ts` uses `useColorMode` as a Nuxt auto-import (no explicit
// import), which is not available in a bare happy-dom environment. We stub the
// real `vue` ref/computed plus a mock `useColorMode` onto globalThis BEFORE the
// module under test evaluates — dynamic import in beforeAll guarantees the
// stubs are in place before the module body runs.
vi.stubGlobal('ref', ref);
vi.stubGlobal('computed', computed);
vi.stubGlobal(
  'useColorMode',
  vi.fn(() => ref('dark'))
);

// The module imports `readBackgroundConfig` / `saveBackground` from
// `@/composables/db`; mock the whole module so no Dexie/IndexedDB is touched.
const mocks = vi.hoisted(() => ({
  readBackgroundConfig: vi.fn(),
  saveBackground: vi.fn(async () => undefined)
}));

vi.mock('@/composables/db', () => mocks);

type ChatBackground = {
  backgroundUrl: { value: string };
  backgroundOpacity: { value: number };
  backgroundLoaded: { value: boolean };
  loadBackground: () => Promise<void>;
  setBackground: (url: string, opacity?: number) => Promise<void>;
  setBackgroundOpacity: (opacity: number) => Promise<void>;
  chatBackgroundStyle: { value: Record<string, unknown> | undefined };
  chatBackgroundOverlayStyle: { value: Record<string, unknown> | undefined };
};

let useChatBackground: () => ChatBackground;

beforeAll(async () => {
  const mod = await import('../useChatBackground');
  useChatBackground = mod.useChatBackground as () => ChatBackground;
});

// Module-level singleton: the three module refs are shared across all calls.
describe('useChatBackground', () => {
  beforeEach(() => {
    mocks.readBackgroundConfig.mockReset();
    mocks.saveBackground.mockReset();
    mocks.saveBackground.mockResolvedValue(undefined);
    // Reset module singleton so state doesn't leak between cases.
    const api = useChatBackground();
    api.backgroundUrl.value = '';
    api.backgroundOpacity.value = 0;
    api.backgroundLoaded.value = false;
    (globalThis as unknown as { useColorMode: ReturnType<typeof vi.fn> }).useColorMode.mockReturnValue(ref('dark'));
  });

  it('starts with default state (no bg, opacity 0, not loaded)', () => {
    const { backgroundUrl, backgroundOpacity, backgroundLoaded } = useChatBackground();
    expect(backgroundUrl.value).toBe('');
    expect(backgroundOpacity.value).toBe(0);
    expect(backgroundLoaded.value).toBe(false);
  });

  it('chatBackgroundStyle returns undefined without a background url', () => {
    const { chatBackgroundStyle } = useChatBackground();
    expect(chatBackgroundStyle.value).toBeUndefined();
  });

  it('chatBackgroundStyle produces a cover background-image when a url is set', () => {
    const api = useChatBackground();
    api.backgroundUrl.value = '/bg/ocean.jpg';
    expect(api.chatBackgroundStyle.value).toEqual({
      backgroundImage: 'url("/bg/ocean.jpg")',
      backgroundSize: 'cover',
      backgroundPosition: 'center',
      backgroundRepeat: 'no-repeat'
    });
  });

  it('chatBackgroundOverlayStyle maps opacity to color and 0..1 value (dark = black)', () => {
    const api = useChatBackground();
    api.backgroundOpacity.value = 40;
    expect(api.chatBackgroundOverlayStyle.value).toEqual({
      backgroundColor: '#000000',
      opacity: 0.4
    });
  });

  it('chatBackgroundOverlayStyle uses white overlay in light mode', () => {
    (globalThis as unknown as { useColorMode: ReturnType<typeof vi.fn> }).useColorMode.mockReturnValue(ref('light'));
    const api = useChatBackground();
    api.backgroundOpacity.value = 100;
    expect(api.chatBackgroundOverlayStyle.value).toEqual({
      backgroundColor: '#ffffff',
      opacity: 1
    });
  });

  it('loadBackground reads Dexie once (idempotent) and fills the singleton', async () => {
    const api = useChatBackground();
    mocks.readBackgroundConfig.mockResolvedValue({
      backgroundUrl: '/bg/green.jpg',
      backgroundOpacity: 60
    });

    await api.loadBackground();
    expect(api.backgroundUrl.value).toBe('/bg/green.jpg');
    expect(api.backgroundOpacity.value).toBe(60);
    expect(api.backgroundLoaded.value).toBe(true);

    // Second call is a no-op (already loaded).
    mocks.readBackgroundConfig.mockResolvedValue({ backgroundUrl: '/bg/other.jpg', backgroundOpacity: 10 });
    await api.loadBackground();
    expect(api.backgroundUrl.value).toBe('/bg/green.jpg');
    expect(mocks.readBackgroundConfig).toHaveBeenCalledTimes(1);
  });

  it('loadBackground falls back to empty values when read fails, but marks loaded', async () => {
    const api = useChatBackground();
    mocks.readBackgroundConfig.mockRejectedValue(new Error('boom'));

    await api.loadBackground();
    expect(api.backgroundUrl.value).toBe('');
    expect(api.backgroundOpacity.value).toBe(0);
    expect(api.backgroundLoaded.value).toBe(true);
  });

  it('setBackground updates state and persists to Dexie (survives persistence failure)', async () => {
    const api = useChatBackground();
    await api.setBackground('/bg/red.jpg', 80);
    expect(api.backgroundUrl.value).toBe('/bg/red.jpg');
    expect(api.backgroundOpacity.value).toBe(80);
    expect(mocks.saveBackground).toHaveBeenCalledWith('/bg/red.jpg', 80);

    // Persistence failure must not throw / not block the in-memory update.
    mocks.saveBackground.mockRejectedValue(new Error('db down'));
    await expect(api.setBackground('/bg/blue.jpg', 20)).resolves.toBeUndefined();
    expect(api.backgroundUrl.value).toBe('/bg/blue.jpg');
    expect(api.backgroundOpacity.value).toBe(20);
  });

  it('setBackground uses current opacity as default when not provided', async () => {
    const api = useChatBackground();
    mocks.saveBackground.mockClear();
    await api.setBackground('/bg/only-url.jpg');
    expect(mocks.saveBackground).toHaveBeenCalledWith('/bg/only-url.jpg', 0);
  });

  it('setBackgroundOpacity updates opacity while preserving the current url', async () => {
    const api = useChatBackground();
    api.backgroundUrl.value = '/bg/keep.jpg';
    await api.setBackgroundOpacity(30);
    expect(api.backgroundOpacity.value).toBe(30);
    expect(mocks.saveBackground).toHaveBeenCalledWith('/bg/keep.jpg', 30);
  });

  it('shares the same singleton state across multiple invocations', () => {
    const a = useChatBackground();
    const b = useChatBackground();
    a.backgroundUrl.value = '/bg/shared.jpg';
    expect(b.backgroundUrl.value).toBe('/bg/shared.jpg');
    expect(b.chatBackgroundStyle.value?.backgroundImage).toBe('url("/bg/shared.jpg")');
  });
});
