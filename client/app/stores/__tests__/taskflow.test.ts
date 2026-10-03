import { describe, it, expect, vi, beforeEach } from 'vitest';
import { setActivePinia } from 'pinia';
import { createTestingPinia } from '@pinia/testing';
import type { FlowProgress } from '../taskflow';
import { useTaskflowStore } from '../taskflow';

const mittMocks = vi.hoisted(() => ({ on: vi.fn(), emit: vi.fn(), off: vi.fn() }));
vi.mock('@/composables/mitt', () => mittMocks);

/**
 * One flow with two waves (wave 1 done, wave 2 open).
 * @param overrides
 */
function makeFlow(overrides: Partial<FlowProgress> = {}): FlowProgress {
  return {
    flow_id: 'flow-1',
    status: 'running',
    description: 'demo flow',
    total: 3,
    done: 2,
    current_wave: 2,
    by_status: { done: 2, ready: 1 },
    waves: [
      {
        index: 1,
        total: 2,
        done: 2,
        by_status: { done: 2 },
        steps: [
          { step_id: 'a', task: 'task a', status: 'done' },
          { step_id: 'b', task: 'task b', status: 'done' }
        ]
      },
      {
        index: 2,
        total: 1,
        done: 0,
        by_status: { ready: 1 },
        steps: [{ step_id: 'c', task: 'task c', status: 'ready' }]
      }
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
    content: { flows, totals: totals ?? { flows: flows.length, total: 3, done: 2, current_wave: 2, waves: 2 } }
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
    reconnectedHandler = mittMocks.on.mock.calls.find(c => c[0] === 'ws:reconnected')?.[1] as (() => void) | undefined;
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
    expect(store.flows[0]!.waves[1]!.steps[0]!.task).toBe('task c');
  });

  it('an empty board clears the panel state', () => {
    pushFrame([makeFlow()]);
    pushFrame([], { flows: 0, total: 0, done: 0, current_wave: 0, waves: 0 });

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

  it('init() pulls a snapshot and a reconnect re-sends the refresh', () => {
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
