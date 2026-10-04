/**
 * Rewind store: the server verdict drives the controls, the cut is posted, and
 * the state refreshes after it lands.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { setActivePinia, createPinia } from 'pinia';
import { useRewindStore } from '../rewind';

const bridgeMocks = vi.hoisted(() => ({
  fetchRewindState: vi.fn(),
  postRewind: vi.fn()
}));
const mittMocks = vi.hoisted(() => ({ on: vi.fn(), emit: vi.fn(), off: vi.fn() }));

vi.mock('@/composables/bridge/session', () => bridgeMocks);
vi.mock('@/composables/mitt', () => mittMocks);

describe('stores/rewind', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    bridgeMocks.fetchRewindState.mockReset();
    bridgeMocks.postRewind.mockReset();
    mittMocks.on.mockClear();
    window.history.replaceState({}, '', '/home/s1');
  });

  it('takes the enable/disable verdict from the server', async () => {
    const store = useRewindStore();
    bridgeMocks.fetchRewindState.mockResolvedValue({ can_rewind: false, branch_generation: 2 });

    await store.refresh();

    expect(store.canRewind).toBe(false);
    expect(store.generation).toBe(2);
    expect(store.enabled).toBe(false);
  });

  it('refreshes on every socket open (registered once)', () => {
    const store = useRewindStore();
    store.subscribe();
    store.subscribe();

    expect(mittMocks.on.mock.calls.filter(c => c[0] === 'ws:connected')).toHaveLength(1);
  });

  it('posts the cut and refreshes the state after it lands', async () => {
    const store = useRewindStore();
    bridgeMocks.fetchRewindState.mockResolvedValue({ can_rewind: true, branch_generation: 0 });
    bridgeMocks.postRewind.mockResolvedValue({ ok: true });
    await store.refresh();

    const ok = await store.rewind(42);

    expect(ok).toBe(true);
    expect(bridgeMocks.postRewind).toHaveBeenCalledWith('s1', 42);
    // Refreshed once on subscribe-time and once after the cut.
    expect(bridgeMocks.fetchRewindState).toHaveBeenCalledTimes(2);
  });

  it('reports a refusal without throwing', async () => {
    const store = useRewindStore();
    bridgeMocks.fetchRewindState.mockResolvedValue({ can_rewind: true, branch_generation: 0 });
    bridgeMocks.postRewind.mockResolvedValue({ ok: false, error: 'a turn is running' });

    const ok = await store.rewind(7);

    expect(ok).toBe(false);
    // A failed cut does not refresh: nothing changed on the server.
    expect(bridgeMocks.fetchRewindState).not.toHaveBeenCalled();
  });

  it('flags the in-flight cut and clears the flag afterwards', async () => {
    const store = useRewindStore();
    bridgeMocks.fetchRewindState.mockResolvedValue({ can_rewind: true, branch_generation: 0 });
    let release: (value: unknown) => void = () => {};
    bridgeMocks.postRewind.mockImplementation(
      () =>
        new Promise(resolve => {
          release = resolve;
        })
    );

    const pending = store.rewind(1);
    expect(store.rewinding).toBe(true);
    expect(store.enabled).toBe(false);
    release({ ok: true });
    await pending;
    expect(store.rewinding).toBe(false);
  });
});
