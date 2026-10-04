/**
 * File-change store: payload application, the chip's arithmetic, the refresh
 * frame, and the revert call (plan vs apply).
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { setActivePinia, createPinia } from 'pinia';
import { useFileChangesStore } from '../file-changes';

const mittMocks = vi.hoisted(() => ({ on: vi.fn(), emit: vi.fn(), off: vi.fn() }));
const apiMocks = vi.hoisted(() => ({ fetchApiPayload: vi.fn() }));

vi.mock('@/composables/mitt', () => mittMocks);
vi.mock('@/composables/requestApi', () => ({
  fetchApiPayload: apiMocks.fetchApiPayload,
  withGatewayToken: (url: string) => url
}));

/**
 * The payload shape the backend pushes and the refresh replies with.
 * @param over
 */
const payload = (over: Record<string, unknown> = {}) => ({
  event: 'file_changes_updated',
  session_id: 's1',
  content: {
    session_id: 's1',
    total_rows: 3,
    changes: [
      { tool_call_id: 'call-1', captured_at: 1, paths: ['/proj/a.txt'] },
      { tool_call_id: 'call-2', captured_at: 2, paths: ['/proj/a.txt', '/proj/b.txt'] }
    ],
    canRevert: true,
    ...over
  }
});

describe('stores/file-changes', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    mittMocks.on.mockClear();
    mittMocks.emit.mockClear();
    apiMocks.fetchApiPayload.mockReset();
    window.history.replaceState({}, '', '/home/s1');
  });

  it('applies a pushed payload and counts distinct files', () => {
    const store = useFileChangesStore();

    store.setPayload(payload());

    expect(store.payload.total_rows).toBe(3);
    // Two rows, but a.txt appears twice: the chip counts FILES.
    expect(store.fileCount).toBe(2);
    expect(store.canRevert).toBe(true);
  });

  it('ignores a payload without content and honours canRevert=false', () => {
    const store = useFileChangesStore();

    store.setPayload({ event: 'file_changes_updated' });
    expect(store.fileCount).toBe(0);
    expect(store.canRevert).toBe(false);

    store.setPayload(payload({ canRevert: false }));
    expect(store.fileCount).toBe(2);
    expect(store.canRevert).toBe(false);
  });

  it('registers its listeners once and refreshes on every socket open', () => {
    const store = useFileChangesStore();
    store.subscribe();
    store.subscribe();

    expect(mittMocks.on.mock.calls.filter(c => c[0] === 'ws:file_changes_updated')).toHaveLength(1);
    expect(mittMocks.on.mock.calls.filter(c => c[0] === 'ws:connected')).toHaveLength(1);

    const onConnected = mittMocks.on.mock.calls.find(c => c[0] === 'ws:connected')?.[1] as () => void;
    onConnected();
    expect(mittMocks.emit).toHaveBeenCalledWith('ws:send', {
      event: 'file_changes_refresh',
      session_id: 's1',
      content: ''
    });
  });

  it('plans with dry_run and applies without it', async () => {
    const store = useFileChangesStore();
    store.setPayload(payload());
    apiMocks.fetchApiPayload.mockResolvedValue({ success: true, files: [] });

    await store.revert({ dryRun: true });
    expect(apiMocks.fetchApiPayload).toHaveBeenCalledWith(
      expect.objectContaining({
        method: 'post',
        opts: expect.objectContaining({ dry_run: true, session_id: 's1' })
      })
    );

    apiMocks.fetchApiPayload.mockResolvedValue({
      success: true,
      files: [{ path: 'a', action: 'restore', safe: true }]
    });
    const applied = await store.revert({ paths: ['/proj/a.txt'] });

    expect(applied?.success).toBe(true);
    expect(apiMocks.fetchApiPayload).toHaveBeenLastCalledWith(
      expect.objectContaining({
        opts: expect.objectContaining({ dry_run: false, paths: ['/proj/a.txt'] })
      })
    );
  });

  it('flags the in-flight revert while it runs', async () => {
    const store = useFileChangesStore();
    store.setPayload(payload());
    let release: (value: unknown) => void = () => {};
    apiMocks.fetchApiPayload.mockImplementation(
      () =>
        new Promise(resolve => {
          release = resolve;
        })
    );

    const pending = store.revert({ dryRun: true });
    expect(store.reverting).toBe(true);
    release({ success: true, files: [] });
    await pending;
    expect(store.reverting).toBe(false);
  });
});
