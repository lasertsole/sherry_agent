import { describe, it, expect, vi, beforeEach, beforeAll } from 'vitest';

// `useSubagentTasks.ts` is a module-level singleton: its refs (taskRuns,
// allTaskRuns, selectedRunIds, focusedRunId, ...) and the module-private
// `subscribed` guard / `lastLoadedSessionId` live outside the exported fn.
//
// Vitest v4 does NOT apply `vi.mock` factories after `vi.resetModules()` +
// per-test dynamic re-import, so we follow the same pattern as the passing
// suites (`useChatBackground.test.ts`, `db.test.ts`): a SINGLE static import
// in `beforeAll` (mocks are applied at module load), then we reset the
// *observable* refs through the API in `beforeEach`. The un-exposed
// `subscribed` / `lastLoadedSessionId` persist across tests, so we design the
// suite to be order-independent w.r.t. those (use distinct sids + rely on the
// first `initTasks` to register WS handlers, which we capture once).

const bridgeMocks = vi.hoisted(() => ({
  fetchSubagentRuns: vi.fn(async () => []),
  fetchSubagentRunSubtree: vi.fn(async () => []),
  deleteSubagentRunSubtree: vi.fn(async () => 1),
}));

const dbMocks = vi.hoisted(() => ({
  cacheSubagentRuns: vi.fn(async () => undefined),
  readCachedSubagentRuns: vi.fn(async () => []),
  deleteCachedSubagentRuns: vi.fn(async () => undefined),
}));

const mittMocks = vi.hoisted(() => ({
  on: vi.fn(),
  off: vi.fn(),
}));

const wsMocks = vi.hoisted(() => ({
  useSubagentWs: vi.fn(),
}));

// `vue-i18n` is aliased to a test stub in vitest.config.ts (it is not
// top-level-resolvable in this workspace), so no `vi.mock` is needed here.
// NOTE: mock specifiers are resolved relative to THIS test file. The source
// (`../useSubagentTasks`) imports `./bridge`/`./db`/`./mitt`/`./ws` from
// `app/composables/`, i.e. `app/composables/bridge.ts` etc. That is exactly what
// `@/composables/*` resolves to (alias `@` → `./app`), and — unlike `./bridge`
// (which would resolve to `app/composables/__tests__/bridge`) — `@/composables/*`
// points at the real module the source imports, so the mock is applied. This
// mirrors the proven `useChatBackground.test.ts` (`vi.mock('@/composables/db')`).
vi.mock('@/composables/mitt', () => mittMocks);
vi.mock('@/composables/bridge', () => bridgeMocks);
vi.mock('@/composables/db', () => dbMocks);
vi.mock('@/composables/ws', () => wsMocks);

import type { SubagentRun } from '../bridge';

type Api = ReturnType<typeof import('../useSubagentTasks').useSubagentTasks>;
let useSubagentTasks: () => Api;

beforeAll(async () => {
  const mod = await import('../useSubagentTasks');
  useSubagentTasks = mod.useSubagentTasks as () => Api;
});

/** Build a minimal SubagentRun fixture. */
function makeRun(overrides: Partial<Omit<SubagentRun, 'run_id'>> & { run_id: string }): SubagentRun {
  return {
    task_run_id: null,
    child_session_key: '',
    requester_session_key: 'session-1',
    task: '',
    task_name: undefined,
    label: undefined,
    spawn_mode: undefined,
    context_mode: undefined,
    agent_id: undefined,
    depth: 1,
    other: undefined,
    execution: { status: 'DONE', started_at: null, ended_at: null, outcome: { status: 'OK', error: null } },
    completion: { required: false, result_text: null, captured_at: null },
    delivery: { status: 'DELIVERED' },
    ...overrides,
  };
}

/** Fully reset the observable singleton state (all refs are exposed via the API). */
function resetSingleton(): void {
  const api = useSubagentTasks();
  api.taskRuns.value = [];
  api.allTaskRuns.value = [];
  api.taskLoading.value = false;
  api.lastTasksFetchedAt.value = 0;
  api.subagentWsReady.value = false;
  api.expandedRunId.value = undefined;
  api.selectedRunId.value = undefined;
  api.focusedRunId.value = undefined;
  api.selectedRunIds.value = new Set();
  api.deletingRunIds.value = new Set();
  api.setTasksTabActive(false);
  // Ensure the module-level `resolveSid()` reads a clean path.
  window.history.replaceState({}, '', '/');
}

describe('useSubagentTasks', () => {
  beforeEach(() => {
    bridgeMocks.fetchSubagentRuns.mockReset().mockResolvedValue([]);
    bridgeMocks.fetchSubagentRunSubtree.mockReset().mockResolvedValue([]);
    bridgeMocks.deleteSubagentRunSubtree.mockReset().mockResolvedValue(1);
    dbMocks.cacheSubagentRuns.mockReset().mockResolvedValue(undefined);
    dbMocks.readCachedSubagentRuns.mockReset().mockResolvedValue([]);
    dbMocks.deleteCachedSubagentRuns.mockReset().mockResolvedValue(undefined);
    mittMocks.on.mockReset();
    mittMocks.off.mockReset();
    wsMocks.useSubagentWs.mockReset().mockReturnValue({ onReconnect: undefined });
    resetSingleton();
  });

  it('exposes empty default state', () => {
    const api = useSubagentTasks();
    expect(api.taskRuns.value).toEqual([]);
    expect(api.allTaskRuns.value).toEqual([]);
    expect(api.taskLoading.value).toBe(false);
    expect(api.runningTaskCount.value).toBe(0);
    expect(api.allRunningTaskCount.value).toBe(0);
    expect(api.subagentWsReady.value).toBe(false);
  });

  describe('status/badge/role labels', () => {
    it('badgeClass colors by execution status and outcome', () => {
      const api = useSubagentTasks();
      const running = makeRun({ run_id: 'r1', execution: { status: 'RUNNING', started_at: 0, ended_at: 0, outcome: null } });
      const interrupted = makeRun({ run_id: 'r2', execution: { status: 'INTERRUPTED', started_at: 0, ended_at: 0, outcome: null } });
      const ok = makeRun({ run_id: 'r3' });
      const err = makeRun({ run_id: 'r4', execution: { status: 'DONE', started_at: 0, ended_at: 0, outcome: { status: 'ERROR', error: null } } });
      const timeout = makeRun({ run_id: 'r5', execution: { status: 'DONE', started_at: 0, ended_at: 0, outcome: { status: 'TIMEOUT', error: null } } });
      const killed = makeRun({ run_id: 'r6', execution: { status: 'DONE', started_at: 0, ended_at: 0, outcome: { status: 'KILLED', error: null } } });
      const unknown = makeRun({ run_id: 'r7', execution: { status: 'UNKNOWN', started_at: 0, ended_at: 0, outcome: null } });

      expect(api.badgeClass(running)).toContain('bg-blue-100');
      expect(api.badgeClass(interrupted)).toContain('bg-amber-100');
      expect(api.badgeClass(ok)).toContain('bg-green-100');
      expect(api.badgeClass(err)).toContain('bg-red-100');
      expect(api.badgeClass(timeout)).toContain('bg-orange-100');
      expect(api.badgeClass(killed)).toContain('bg-orange-100');
      expect(api.badgeClass(unknown)).toContain('bg-gray-100');
    });

    it('statusLabel prioritizes execution, then delivery, then outcome', () => {
      const api = useSubagentTasks();
      const running = makeRun({ run_id: 'r1', execution: { status: 'RUNNING', started_at: 0, ended_at: 0, outcome: null } });
      const interrupted = makeRun({ run_id: 'r2', execution: { status: 'INTERRUPTED', started_at: 0, ended_at: 0, outcome: null } });
      const pending = makeRun({ run_id: 'r3', execution: { status: 'SCHEDULED', started_at: 0, ended_at: 0, outcome: null }, delivery: { status: 'PENDING' } });
      const inProgress = makeRun({ run_id: 'r4', execution: { status: 'SCHEDULED', started_at: 0, ended_at: 0, outcome: null }, delivery: { status: 'IN_PROGRESS' } });
      const delivered = makeRun({ run_id: 'r5', delivery: { status: 'DELIVERED' } });
      const done = makeRun({ run_id: 'r6', delivery: { status: 'NONE' } });
      const error = makeRun({ run_id: 'r7', execution: { status: 'DONE', started_at: 0, ended_at: 0, outcome: { status: 'ERROR', error: null } }, delivery: { status: 'NONE' } });
      const timeout = makeRun({ run_id: 'r8', execution: { status: 'DONE', started_at: 0, ended_at: 0, outcome: { status: 'TIMEOUT', error: null } }, delivery: { status: 'NONE' } });
      const killed = makeRun({ run_id: 'r9', execution: { status: 'DONE', started_at: 0, ended_at: 0, outcome: { status: 'KILLED', error: null } }, delivery: { status: 'NONE' } });
      const unknown = makeRun({ run_id: 'r10', execution: { status: 'DONE', started_at: 0, ended_at: 0, outcome: { status: 'WEIRD', error: null } }, delivery: { status: 'NONE' } });

      expect(api.statusLabel(running)).toBe('sidebar.statusRunning');
      expect(api.statusLabel(interrupted)).toBe('sidebar.statusInterrupted');
      expect(api.statusLabel(pending)).toBe('sidebar.statusPending');
      expect(api.statusLabel(inProgress)).toBe('sidebar.statusInProgress');
      expect(api.statusLabel(delivered)).toBe('sidebar.statusDelivered');
      expect(api.statusLabel(done)).toBe('sidebar.statusDone');
      expect(api.statusLabel(error)).toBe('sidebar.statusError');
      expect(api.statusLabel(timeout)).toBe('sidebar.statusTimeout');
      expect(api.statusLabel(killed)).toBe('sidebar.statusKilled');
      expect(api.statusLabel(unknown)).toBe('sidebar.statusUnknown');
    });

    it('roleLabel uses depth to choose root vs child', () => {
      const api = useSubagentTasks();
      expect(api.roleLabel(makeRun({ run_id: 'r1', depth: 0 }))).toBe('sidebar.roleRoot');
      expect(api.roleLabel(makeRun({ run_id: 'r2' }))).toBe('sidebar.roleChild#1');
      expect(api.roleLabel(makeRun({ run_id: 'r3', depth: 3 }))).toBe('sidebar.roleChild#3');
    });

    it('runLabel falls through label -> task_name -> task -> run_id, then parentSessionLabel', () => {
      const api = useSubagentTasks();
      expect(api.runLabel(makeRun({ run_id: 'r1', label: 'L', task_name: 'TN', task: 'T' }))).toBe('L');
      expect(api.runLabel(makeRun({ run_id: 'r2', label: '', task_name: 'TN', task: 'T' }))).toBe('TN');
      expect(api.runLabel(makeRun({ run_id: 'r3', label: '', task_name: '', task: 'T' }))).toBe('T');
      expect(api.runLabel(makeRun({ run_id: 'r4', label: '', task_name: '', task: '' }))).toBe('r4');
      expect(api.parentSessionLabel(makeRun({ run_id: 'r5', requester_session_key: 'S' }))).toBe('S');
    });
  });

  describe('grouping / subtree computed', () => {
    it('rootTaskRuns only keeps depth===1 and groupedRootTaskRuns clusters by session', () => {
      const api = useSubagentTasks();
      api.allTaskRuns.value = [
        makeRun({ run_id: 'root-1', depth: 1, requester_session_key: 'A' }),
        makeRun({ run_id: 'root-2', depth: 1, requester_session_key: 'B' }),
        makeRun({ run_id: 'root-3', depth: 1, requester_session_key: 'A' }),
        makeRun({ run_id: 'deep', depth: 2, requester_session_key: 'A' }),
      ];
      expect(api.rootTaskRuns.value.map(r => r.run_id)).toEqual(['root-1', 'root-2', 'root-3']);
      const groups = api.groupedRootTaskRuns.value;
      expect(groups.map(g => g.sessionId)).toEqual(['A', 'B']);
      expect(groups[0].runs.map(r => r.run_id)).toEqual(['root-1', 'root-3']);
    });

    it('focusedSubtreeRuns without focus returns rootTaskRuns', () => {
      const api = useSubagentTasks();
      api.allTaskRuns.value = [
        makeRun({ run_id: 'r1', depth: 1 }),
        makeRun({ run_id: 'r2', depth: 2, requester_session_key: 'child-sess' }),
      ];
      expect(api.focusedSubtreeRuns.value.map(r => r.run_id)).toEqual(['r1']);
    });
  });

  describe('expanded / focus flow state', () => {
    it('toggleExpandRun expands and collapses a run id (syncing selection)', () => {
      const api = useSubagentTasks();
      api.toggleExpandRun('r1');
      expect(api.expandedRunId.value).toBe('r1');
      expect(api.selectedRunId.value).toBe('r1');

      api.toggleExpandRun('r1');
      expect(api.expandedRunId.value).toBeUndefined();
      expect(api.selectedRunId.value).toBeUndefined();
    });

    it('focusRun sets expanded/selected/focused; resetFlowState clears expanded but keeps focused', () => {
      const api = useSubagentTasks();
      api.focusRun('r2');
      expect(api.expandedRunId.value).toBe('r2');
      expect(api.selectedRunId.value).toBe('r2');
      expect(api.focusedRunId.value).toBe('r2');

      api.resetFlowState();
      expect(api.expandedRunId.value).toBeUndefined();
      expect(api.selectedRunId.value).toBeUndefined();
      expect(api.focusedRunId.value).toBe('r2');
    });
  });

  describe('load / refresh', () => {
    it('loadTaskRuns shows local cache first, then fetches server and repopulates', async () => {
      const api = useSubagentTasks();
      const cached = [
        makeRun({ run_id: 'c1', depth: 1, requester_session_key: 'session-1' }),
        makeRun({ run_id: 'c2', depth: 2, requester_session_key: 'other-sess' }),
      ];
      dbMocks.readCachedSubagentRuns.mockResolvedValue(cached as never);
      bridgeMocks.fetchSubagentRuns.mockResolvedValue([
        makeRun({ run_id: 's1', depth: 1, requester_session_key: 'session-1' }),
      ] as never);

      await api.loadTaskRuns('session-1');

      expect(api.allTaskRuns.value.map(r => r.run_id)).toEqual(['c1', 'c2']);
      expect(api.taskRuns.value.map(r => r.run_id)).toEqual(['c1']);
      expect(bridgeMocks.fetchSubagentRuns).toHaveBeenCalledWith('session-1', 'descendants');
      expect(api.taskLoading.value).toBe(false);
      expect(api.lastTasksFetchedAt.value).toBeGreaterThan(0);
    });

    it('loadTaskRuns without a target sid clears taskRuns but keeps the global list', async () => {
      const api = useSubagentTasks();
      dbMocks.readCachedSubagentRuns.mockResolvedValue([
        makeRun({ run_id: 'c1', depth: 1, requester_session_key: 'session-1' }),
      ] as never);

      await api.loadTaskRuns();
      expect(api.taskRuns.value).toEqual([]);
      expect(api.allTaskRuns.value.map(r => r.run_id)).toEqual(['c1']);
      expect(bridgeMocks.fetchSubagentRuns).not.toHaveBeenCalled();
    });
  });

  describe('subtree refresh', () => {
    it('refreshFocusedSubtree with no focus does not fetch a subtree', async () => {
      const api = useSubagentTasks();
      await api.refreshFocusedSubtree();
      expect(bridgeMocks.fetchSubagentRunSubtree).not.toHaveBeenCalled();
    });

    it('refreshFocusedSubtree fetches the focused subtree, caches it, and rebuilds', async () => {
      const api = useSubagentTasks();
      api.focusRun('root-1');
      bridgeMocks.fetchSubagentRunSubtree.mockResolvedValue([
        makeRun({ run_id: 'root-1', depth: 1 }),
        makeRun({ run_id: 'leaf', depth: 2, requester_session_key: 'child-sess' }),
      ] as never);
      dbMocks.readCachedSubagentRuns.mockResolvedValue([
        makeRun({ run_id: 'root-1', depth: 1 }),
      ] as never);

      await api.refreshFocusedSubtree();
      expect(bridgeMocks.fetchSubagentRunSubtree).toHaveBeenCalledWith('root-1');
      expect(dbMocks.cacheSubagentRuns).toHaveBeenCalled();
      expect(dbMocks.readCachedSubagentRuns).toHaveBeenCalled();
      expect(api.allTaskRuns.value.map(r => r.run_id)).toEqual(['root-1']);
    });
  });

  describe('WS setup', () => {
    // The singleton only registers WS handlers on the FIRST `initTasks` call
    // (`subscribed` guard). We do that once and keep the captured handlers for
    // the following cases so they are order-independent.
    let spawnedHandler: ((payload: unknown) => void) | undefined;
    let endedHandler: ((payload: unknown) => void) | undefined;
    let readyHandler: (() => void) | undefined;

    it('initTasks registers the three WS handlers once (singleton guard)', () => {
      const api = useSubagentTasks();
      api.initTasks('session-1');
      expect(wsMocks.useSubagentWs).toHaveBeenCalledTimes(1);
      expect(mittMocks.on).toHaveBeenCalledWith('ws:subagent_spawned', expect.any(Function));
      expect(mittMocks.on).toHaveBeenCalledWith('ws:subagent_ended', expect.any(Function));
      expect(mittMocks.on).toHaveBeenCalledWith('ws:subagents:ready', expect.any(Function));

      spawnedHandler = mittMocks.on.mock.calls.find(([name]) => name === 'ws:subagent_spawned')?.[1] as
        | ((p: unknown) => void)
        | undefined;
      endedHandler = mittMocks.on.mock.calls.find(([name]) => name === 'ws:subagent_ended')?.[1] as
        | ((p: unknown) => void)
        | undefined;
      readyHandler = mittMocks.on.mock.calls.find(([name]) => name === 'ws:subagents:ready')?.[1] as
        | (() => void)
        | undefined;

      // Calling initTasks again must NOT re-subscribe (module-level guard).
      api.initTasks('session-2');
      expect(wsMocks.useSubagentWs).toHaveBeenCalledTimes(1);
    });

    it('spawned handler caches the new run and refreshes the list', async () => {
      const api = useSubagentTasks();
      expect(spawnedHandler).toBeDefined();
      dbMocks.readCachedSubagentRuns.mockResolvedValue([
        makeRun({ run_id: 'new-1', depth: 1, requester_session_key: 'session-1' }),
      ] as never);

      spawnedHandler!({ run_id: 'new-1' });

      await vi.waitFor(() => {
        expect(dbMocks.cacheSubagentRuns).toHaveBeenCalled();
        expect(api.allTaskRuns.value.map(r => r.run_id)).toContain('new-1');
      });
    });

    it('ended handler caches the terminal run and refreshes the list', async () => {
      const api = useSubagentTasks();
      expect(endedHandler).toBeDefined();
      dbMocks.readCachedSubagentRuns.mockResolvedValue([
        makeRun({ run_id: 'ended-1', depth: 1, requester_session_key: 'session-1' }),
      ] as never);

      endedHandler!({ run_id: 'ended-1' });

      await vi.waitFor(() => {
        expect(dbMocks.cacheSubagentRuns).toHaveBeenCalled();
        expect(api.allTaskRuns.value.map(r => r.run_id)).toContain('ended-1');
      });
    });

    it('spawned handler ignores payloads without a run_id', async () => {
      const api = useSubagentTasks();
      spawnedHandler!(null);
      spawnedHandler!({});
      // No cache write, list stays empty.
      expect(dbMocks.cacheSubagentRuns).not.toHaveBeenCalled();
      expect(api.allTaskRuns.value).toEqual([]);
    });

    it('ready handler marks ws ready; fetches only when the tasks tab is active', async () => {
      const api = useSubagentTasks();
      expect(readyHandler).toBeDefined();

      // Not active → ready flag set, no fetch.
      readyHandler!();
      expect(api.subagentWsReady.value).toBe(true);
      expect(bridgeMocks.fetchSubagentRuns).not.toHaveBeenCalled();

      // Active tab → triggers a load that reaches fetchSubagentRuns (pathname sid).
      api.setTasksTabActive(true);
      window.history.pushState({}, '', '/session-9');
      bridgeMocks.fetchSubagentRuns.mockResolvedValue([] as never);
      dbMocks.readCachedSubagentRuns.mockResolvedValue([] as never);

      readyHandler!();
      await vi.waitFor(() => {
        expect(bridgeMocks.fetchSubagentRuns).toHaveBeenCalled();
      });
    });
  });

  describe('selection helpers', () => {
    it('toggleSelectAllTasks / toggleTaskSelection / allSelected / someSelected', () => {
      const api = useSubagentTasks();
      api.allTaskRuns.value = [
        makeRun({ run_id: 'root-1', depth: 1 }),
        makeRun({ run_id: 'root-2', depth: 1 }),
        makeRun({ run_id: 'deep', depth: 2 }),
      ];
      // `selectableRunIds` is module-private; its behavior (only depth===1
      // roots are selectable) is validated indirectly below via the toggle.
      expect(api.allSelected.value).toBe(false);
      expect(api.someSelected.value).toBe(false);

      api.toggleSelectAllTasks();
      expect(api.selectedRunIds.value.has('deep')).toBe(false);
      expect(api.allSelected.value).toBe(true);
      expect(api.someSelected.value).toBe(false);

      // Deselect one → someSelected true, allSelected false.
      api.toggleTaskSelection('root-1');
      expect(api.selectedRunIds.value.has('root-1')).toBe(false);
      expect(api.allSelected.value).toBe(false);
      expect(api.someSelected.value).toBe(true);

      // Re-select, then toggle all again → deselect all.
      api.toggleTaskSelection('root-1');
      api.toggleSelectAllTasks();
      expect(api.selectedRunIds.value.size).toBe(0);
      expect(api.allSelected.value).toBe(false);
    });

    it('clearTaskSelection empties the selected set', () => {
      const api = useSubagentTasks();
      api.allTaskRuns.value = [makeRun({ run_id: 'root-1', depth: 1 })];
      api.toggleSelectAllTasks();
      api.clearTaskSelection();
      expect(api.selectedRunIds.value.size).toBe(0);
    });
  });

  describe('delete flow', () => {
    it('deleteSubagentSubtree removes run + descendants from store and Dexie', async () => {
      const api = useSubagentTasks();
      api.allTaskRuns.value = [
        makeRun({ run_id: 'root-1', depth: 1, child_session_key: 'cs' }),
        makeRun({ run_id: 'child-1', depth: 2, requester_session_key: 'cs' }),
        makeRun({ run_id: 'keep-1', depth: 1 }),
      ];
      api.taskRuns.value = [...api.allTaskRuns.value];
      api.focusRun('root-1');

      await api.deleteSubagentSubtree('root-1');

      expect(bridgeMocks.deleteSubagentRunSubtree).toHaveBeenCalledWith('root-1');
      expect(dbMocks.deleteCachedSubagentRuns).toHaveBeenCalledWith(['root-1', 'child-1']);
      expect(api.allTaskRuns.value.map(r => r.run_id)).toEqual(['keep-1']);
      expect(api.taskRuns.value.map(r => r.run_id)).toEqual(['keep-1']);
      expect(api.focusedRunId.value).toBeUndefined();
      expect(api.deletingRunIds.value.has('root-1')).toBe(false);
    });

    it('deleteSelectedTasks deletes each selected root and returns the count', async () => {
      const api = useSubagentTasks();
      api.allTaskRuns.value = [
        makeRun({ run_id: 'root-1', depth: 1 }),
        makeRun({ run_id: 'root-2', depth: 1 }),
        makeRun({ run_id: 'keep', depth: 1 }),
      ];
      api.taskRuns.value = [...api.allTaskRuns.value];
      api.toggleSelectAllTasks(); // selects root-1, root-2, keep
      api.toggleTaskSelection('keep'); // drop keep

      const removed = await api.deleteSelectedTasks();
      expect(removed).toBe(2);
      expect(api.allTaskRuns.value.map(r => r.run_id)).toEqual(['keep']);
      expect(api.selectedRunIds.value.size).toBe(0);
    });
  });
});
