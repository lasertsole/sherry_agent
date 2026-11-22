import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { setActivePinia } from 'pinia';
import { createTestingPinia } from '@pinia/testing';
import type { FlowProgress } from '../taskflow';
import { useTaskflowStore } from '../taskflow';

const mittMocks = vi.hoisted(() => ({ on: vi.fn(), emit: vi.fn(), off: vi.fn() }));
vi.mock('@/composables/mitt', () => mittMocks);

/**
 * One flow with three flat steps (two done, one ready).
 * @param overrides
 */
function makeFlow(overrides: Partial<FlowProgress> = {}): FlowProgress {
  return {
    flow_id: 'flow-1',
    status: 'running',
    description: 'demo flow',
    total: 3,
    done: 2,
    by_status: { done: 2, ready: 1 },
    steps: [
      { step_id: 'a', task: 'task a', status: 'done' },
      { step_id: 'b', task: 'task b', status: 'done' },
      { step_id: 'c', task: 'task c', status: 'ready' }
    ],
    ...overrides
  };
}

let store: ReturnType<typeof useTaskflowStore>;
let updatedHandler: ((payload: unknown) => void) | undefined;
let reconnectedHandler: (() => void) | undefined;

/**
 * Push a raw frame into the captured `ws:taskflow_updated` handler.
 * @param flows
 * @param totals
 */
function pushFrame(flows: FlowProgress[], totals?: Record<string, number>): void {
  updatedHandler!({
    event: 'taskflow_updated',
    session_id: 'sid-1',
    content: { flows, totals: totals ?? { flows: flows.length, total: 3, done: 2 } }
  });
}

describe('stores/taskflow', () => {
  beforeEach(() => {
    mittMocks.on.mockClear();
    mittMocks.emit.mockClear();
    setActivePinia(createTestingPinia({ stubActions: false }));
    store = useTaskflowStore();
    store.subscribe();
    updatedHandler = mittMocks.on.mock.calls.find(c => c[0] === 'ws:taskflow_updated')?.[1] as
      ((payload: unknown) => void) | undefined;
    reconnectedHandler = mittMocks.on.mock.calls.find(c => c[0] === 'ws:connected')?.[1] as (() => void) | undefined;
  });

  it('registers its listeners exactly once', () => {
    store.subscribe();
    expect(mittMocks.on).toHaveBeenCalledTimes(2);
    expect(store.subscribed).toBe(true);
  });

  it('applies a pushed payload (flows + aggregate)', () => {
    pushFrame([makeFlow()]);

    expect(store.flows).toHaveLength(1);
    expect(store.totals.done).toBe(2);
    expect(store.percent).toBe(67);
    expect(store.hasProgress).toBe(true);
    expect(store.flows[0]!.steps[2]!.task).toBe('task c');
  });

  it('an empty board clears the panel state', () => {
    pushFrame([makeFlow()]);
    pushFrame([], { flows: 0, total: 0, done: 0 });

    expect(store.hasProgress).toBe(false);
    expect(store.percent).toBe(0);
  });

  it('tolerates a malformed payload without dropping the last good state shape', () => {
    updatedHandler!({ event: 'taskflow_updated', content: null });
    expect(store.flows).toEqual([]);

    pushFrame([makeFlow()]);
    updatedHandler!({ nonsense: true });
    expect(store.flows).toEqual([]);
  });

  it('refresh() sends a taskflow_refresh frame for the session', () => {
    store.refresh('sid-9');

    expect(mittMocks.emit).toHaveBeenCalledWith('ws:send', {
      event: 'taskflow_refresh',
      session_id: 'sid-9',
      content: ''
    });
    expect(store.currentSid).toBe('sid-9');
  });

  it('refresh() without a session is a no-op (no frame, no throw)', () => {
    store.setCurrentSid('');
    store.refresh('');

    expect(mittMocks.emit).not.toHaveBeenCalled();
  });

  it('init() pulls a snapshot, and every socket open re-sends the refresh', () => {
    store.init('sid-r');
    expect(mittMocks.emit).toHaveBeenCalledWith('ws:send', {
      event: 'taskflow_refresh',
      session_id: 'sid-r',
      content: ''
    });

    mittMocks.emit.mockClear();
    reconnectedHandler!();
    expect(mittMocks.emit).toHaveBeenCalledWith('ws:send', {
      event: 'taskflow_refresh',
      session_id: 'sid-r',
      content: ''
    });
  });

  it('clear() drops the board (session switch)', () => {
    pushFrame([makeFlow()]);
    store.clear();

    expect(store.flows).toEqual([]);
    expect(store.hasProgress).toBe(false);
  });
});

/** UI upgrade S3: the global cross-session board (task-board nav panel). */
describe('stores/taskflow — global board (S3)', () => {
  const globalFlow = (overrides: Partial<FlowProgress> = {}): FlowProgress =>
    makeFlow({ flow_id: 'g-1', status: 'running', session_id: 'sess-a', description: 'global flow', ...overrides });

  beforeEach(() => {
    setActivePinia(createTestingPinia({ stubActions: false }));
    store = useTaskflowStore();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('loadAllFlows() loads the global list and derives the running badge count', async () => {
    vi.stubGlobal(
      '$fetch',
      vi.fn().mockResolvedValue({
        flows: [globalFlow(), globalFlow({ flow_id: 'g-2', status: 'done' })]
      })
    );

    await store.loadAllFlows();

    expect(store.globalFlows).toHaveLength(2);
    expect(store.runningCount).toBe(1);
    expect(store.globalLoading).toBe(false);
  });

  it('filteredGlobalFlows filters by status and returns all for "all"', async () => {
    vi.stubGlobal(
      '$fetch',
      vi.fn().mockResolvedValue({
        flows: [globalFlow({ status: 'running' }), globalFlow({ flow_id: 'g-2', status: 'failed' })]
      })
    );
    await store.loadAllFlows();

    expect(store.filteredGlobalFlows).toHaveLength(2);
    store.globalFilter = 'failed';
    expect(store.filteredGlobalFlows.map(f => f.flow_id)).toEqual(['g-2']);
    store.globalFilter = 'all';
    expect(store.filteredGlobalFlows).toHaveLength(2);
  });

  it('fails open to an empty list when the endpoint is missing', async () => {
    vi.stubGlobal('$fetch', vi.fn().mockRejectedValue(new Error('404')));

    await store.loadAllFlows();

    expect(store.globalFlows).toEqual([]);
    expect(store.globalLoading).toBe(false);
  });
});
