import { beforeEach, describe, expect, it, vi } from 'vitest';

const apiMock = vi.hoisted(() => ({ fetchApi: vi.fn() }));
vi.mock('../requestApi', () => apiMock);

import { useBackgroundTasks } from '../use-background-tasks';

/** The session socket surface the composable uses (one method). */
function fakeSocket() {
  return { sendTerminalBgKill: vi.fn() } as unknown as Parameters<typeof useBackgroundTasks>[1] & {
    sendTerminalBgKill: ReturnType<typeof vi.fn>;
  };
}

const task = (over: Partial<Record<string, unknown>> = {}) => ({
  task_id: 'bg-1',
  command: 'sleep 1000',
  status: 'running',
  pid: 4242,
  exit_code: null,
  ...over
});

describe('useBackgroundTasks', () => {
  beforeEach(() => {
    apiMock.fetchApi.mockReset();
    apiMock.fetchApi.mockResolvedValue({ tasks: [] });
  });

  it('loads the session snapshot from GET /terminal/bg', async () => {
    apiMock.fetchApi.mockResolvedValue({ tasks: [task(), task({ task_id: 'bg-2', status: 'completed' })] });
    const composable = useBackgroundTasks(() => 'sess-1', fakeSocket());

    await composable.load();

    // The trailing `_` is the cache-buster: a snapshot must never be answered
    // from the browser cache (a re-pull right after a kill would read stale).
    expect(apiMock.fetchApi).toHaveBeenCalledTimes(1);
    const call = apiMock.fetchApi.mock.calls[0]?.[0] as { url: string; method: string };
    expect(call.method).toBe('get');
    expect(call.url).toMatch(/^\/terminal\/bg\?session_id=sess-1&_=\d+$/);
    expect(composable.tasks.value.map(t => t.taskId)).toEqual(['bg-1', 'bg-2']);
    expect(composable.runningCount.value).toBe(1);
  });

  it('leaves the list alone when the snapshot fails', async () => {
    const composable = useBackgroundTasks(() => 'sess-1', fakeSocket());
    composable.onStarted(task() as never);
    apiMock.fetchApi.mockRejectedValue(new Error('offline'));

    await composable.load();

    expect(composable.tasks.value).toHaveLength(1);
  });

  it('drops a settled task from the in-flight stop set when the snapshot says so', async () => {
    const composable = useBackgroundTasks(() => 'sess-1', fakeSocket());
    composable.onStarted(task() as never);
    composable.kill('bg-1');
    expect(composable.killing.value.has('bg-1')).toBe(true);

    apiMock.fetchApi.mockResolvedValue({ tasks: [task({ status: 'killed', exit_code: -15 })] });
    await composable.load();

    // The row must show the real terminal status, not the requested stop.
    expect(composable.killing.value.has('bg-1')).toBe(false);
    expect(composable.tasks.value[0]!.status).toBe('killed');
  });

  it('adds a started row and updates a repeated start in place', () => {
    const composable = useBackgroundTasks(() => 'sess-1', fakeSocket());

    composable.onStarted(task() as never);
    composable.onStarted(task({ status: 'running', pid: 5252 }) as never);

    expect(composable.tasks.value).toHaveLength(1);
    expect(composable.tasks.value[0]!.pid).toBe(5252);
  });

  it('settles a row on exit and clears its pending kill', () => {
    const socket = fakeSocket();
    const composable = useBackgroundTasks(() => 'sess-1', socket);
    composable.onStarted(task() as never);
    composable.kill('bg-1');
    expect(composable.killing.value.has('bg-1')).toBe(true);

    composable.onExit({ taskId: 'bg-1', status: 'killed', reason: 'killed', exitCode: -15 });

    const row = composable.tasks.value[0]!;
    expect(row.status).toBe('killed');
    expect(row.exitCode).toBe(-15);
    expect(row.reason).toBe('killed');
    expect(composable.killing.value.has('bg-1')).toBe(false);
  });

  it('sends one kill frame per task and marks it pending', () => {
    const socket = fakeSocket();
    const composable = useBackgroundTasks(() => 'sess-1', socket);

    composable.kill('bg-9');

    expect(socket.sendTerminalBgKill).toHaveBeenCalledWith('bg-9');
    expect(composable.killing.value.has('bg-9')).toBe(true);
  });

  it('surfaces a refused kill and stops showing it as pending', () => {
    const composable = useBackgroundTasks(() => 'sess-1', fakeSocket());
    composable.kill('bg-1');

    composable.onKillAck({ taskId: 'bg-1', ok: false, reason: 'not_found_or_not_running' });

    expect(composable.killing.value.has('bg-1')).toBe(false);
    expect(composable.killRefusal.value).toEqual({
      taskId: 'bg-1',
      reason: 'not_found_or_not_running'
    });
  });

  it('keeps the row pending when the kill was acknowledged', () => {
    const composable = useBackgroundTasks(() => 'sess-1', fakeSocket());
    composable.kill('bg-1');

    composable.onKillAck({ taskId: 'bg-1', ok: true });

    expect(composable.killing.value.has('bg-1')).toBe(true);
    expect(composable.killRefusal.value).toBeNull();
  });

  it('adds a row for an exit frame it never saw start', () => {
    const composable = useBackgroundTasks(() => 'sess-1', fakeSocket());

    composable.onExit({ taskId: 'bg-ghost', status: 'completed', reason: 'exited', exitCode: 0 });

    expect(composable.tasks.value.map(t => t.taskId)).toEqual(['bg-ghost']);
  });
});
