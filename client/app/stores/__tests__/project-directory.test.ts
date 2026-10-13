/**
 * Per-session project directory store (toolbar folder chip).
 *
 * The backend owns the truth; the store mirrors it optimistically and corrects
 * itself from the response. The load-bearing behaviours: a mid-turn choice is
 * parked (the live value must NOT move until the boundary), a rejection rolls
 * back and keeps its reason for the popover, and sessions are bucketed so one
 * session's root never leaks into another's chip.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { setActivePinia } from 'pinia';
import { createTestingPinia } from '@pinia/testing';
import { useProjectDirectoryStore } from '../project-directory';

const bridge = vi.hoisted(() => ({
  fetchProjectDirectory: vi.fn(),
  setProjectDirectory: vi.fn()
}));

vi.mock('~/composables/bridge/session', () => bridge);

const state = (dir: string | null, pending: string | null = null) => ({
  directory: dir,
  effective: dir ?? '/repo',
  source: dir ? 'session' : 'default',
  pendingDirectory: pending
});

describe('stores/project-directory', () => {
  beforeEach(() => {
    setActivePinia(createTestingPinia({ stubActions: false }));
    bridge.fetchProjectDirectory.mockReset();
    bridge.setProjectDirectory.mockReset();
  });

  it('defaults to unbound before any read', () => {
    const store = useProjectDirectoryStore();

    expect(store.stateFor('sid-1').directory).toBeNull();
    expect(store.stateFor('sid-1').source).toBe('default');
  });

  it('hydrate() mirrors the backend binding', async () => {
    const store = useProjectDirectoryStore();
    bridge.fetchProjectDirectory.mockResolvedValueOnce(state('/p/a'));

    await store.hydrate('sid-1');

    expect(store.stateFor('sid-1').directory).toBe('/p/a');
    expect(store.stateFor('sid-1').error).toBeNull();
  });

  it('hydrate() keeps the last known value when the read fails', async () => {
    const store = useProjectDirectoryStore();
    bridge.fetchProjectDirectory.mockResolvedValueOnce(state('/p/a'));
    await store.hydrate('sid-1');

    bridge.fetchProjectDirectory.mockRejectedValueOnce(new Error('offline'));
    await store.hydrate('sid-1');

    expect(store.stateFor('sid-1').directory).toBe('/p/a');
  });

  it('select() applies a live switch from the response', async () => {
    const store = useProjectDirectoryStore();
    bridge.setProjectDirectory.mockResolvedValueOnce({
      ok: true,
      pending: false,
      state: state('/p/b')
    });

    await store.select('sid-1', '/p/b');

    expect(bridge.setProjectDirectory).toHaveBeenCalledWith('sid-1', '/p/b');
    expect(store.stateFor('sid-1').directory).toBe('/p/b');
  });

  it('a parked choice does not move the live value', async () => {
    const store = useProjectDirectoryStore();
    bridge.fetchProjectDirectory.mockResolvedValueOnce(state('/p/a'));
    await store.hydrate('sid-1');
    bridge.setProjectDirectory.mockResolvedValueOnce({
      ok: true,
      pending: true,
      state: state('/p/a', '/p/b')
    });

    await store.select('sid-1', '/p/b');

    expect(store.stateFor('sid-1').directory).toBe('/p/a');
    expect(store.stateFor('sid-1').pendingDirectory).toBe('/p/b');
  });

  it('a failed write rolls back and surfaces the reason', async () => {
    const store = useProjectDirectoryStore();
    bridge.fetchProjectDirectory.mockResolvedValueOnce(state('/p/a'));
    await store.hydrate('sid-1');
    bridge.setProjectDirectory.mockRejectedValueOnce(new Error('directory does not exist'));

    await store.select('sid-1', '/gone');

    expect(store.stateFor('sid-1').directory).toBe('/p/a');
    expect(store.stateFor('sid-1').pendingDirectory).toBeNull();
    expect(store.stateFor('sid-1').error).toContain('does not exist');
  });

  it('clearError()/fail() drive the popover message', () => {
    const store = useProjectDirectoryStore();

    store.fail('sid-1', 'absolute path required');
    expect(store.stateFor('sid-1').error).toBe('absolute path required');

    store.clearError('sid-1');
    expect(store.stateFor('sid-1').error).toBeNull();
  });

  it('buckets the state per session', async () => {
    const store = useProjectDirectoryStore();
    bridge.fetchProjectDirectory.mockResolvedValueOnce(state('/p/a')).mockResolvedValueOnce(state('/p/b'));

    await store.hydrate('sid-1');
    await store.hydrate('sid-2');

    expect(store.stateFor('sid-1').directory).toBe('/p/a');
    expect(store.stateFor('sid-2').directory).toBe('/p/b');
  });

  it('ignores an empty session id', async () => {
    const store = useProjectDirectoryStore();

    await store.hydrate('');
    await store.select('', '/p/a');

    expect(bridge.fetchProjectDirectory).not.toHaveBeenCalled();
    expect(bridge.setProjectDirectory).not.toHaveBeenCalled();
  });
});
