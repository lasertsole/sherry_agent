import { describe, it, expect, vi, beforeEach, beforeAll } from 'vitest';

// `useTodoList.ts` is a module-level singleton: its refs and the module-private
// `subscribed` guard live outside the exported fn. As with
// `useSubagentTasks.test.ts`, we take a single static-ish dynamic import in
// `beforeAll` (after the mocks are applied), register the WS handlers once via
// the first `useTodoList()` call, and capture those handlers for the rest of the
// suite. Observable state is reset through the public API in `beforeEach`.
const mittMocks = vi.hoisted(() => ({ on: vi.fn(), emit: vi.fn(), off: vi.fn() }));

const uiMock = vi.hoisted(() => {
  const state: { todoDockCollapsed: boolean; toggleTodoDock: () => void } = {
    todoDockCollapsed: false,
    toggleTodoDock: () => {}
  };
  state.toggleTodoDock = () => {
    state.todoDockCollapsed = !state.todoDockCollapsed;
  };
  return state;
});

vi.mock('@/composables/mitt', () => mittMocks);

import type { Todo } from '../use-todo-list';

type Api = ReturnType<typeof import('../use-todo-list').useTodoList>;
let useTodoList: () => Api;

beforeAll(async () => {
  vi.stubGlobal('useUiStore', () => uiMock);
  const mod = await import('../use-todo-list');
  useTodoList = mod.useTodoList as () => Api;
});

/**
 * Minimal valid todo fixture.
 * @param content Todo text.
 * @param overrides Fields overriding the defaults.
 * @returns A valid Todo object.
 */
function makeTodo(content: string, overrides: Partial<Todo> = {}): Todo {
  return { content, status: 'pending', priority: 'medium', ...overrides };
}

/**
 * Push a raw `todo_updated` frame into the captured handler.
 * @param contents
 */
function pushFrame(contents: Todo[]): void {
  todoUpdatedHandler!({ event: 'todo_updated', content: { todos: contents } });
}

let todoUpdatedHandler: ((payload: unknown) => void) | undefined;
let reconnectedHandler: (() => void) | undefined;

describe('useTodoList', () => {
  beforeEach(() => {
    mittMocks.emit.mockReset();
    uiMock.todoDockCollapsed = false;
    const api = useTodoList();
    api.todos.value = [];
    if (!todoUpdatedHandler) {
      todoUpdatedHandler = mittMocks.on.mock.calls.find(c => c[0] === 'ws:todo_updated')?.[1] as
        ((payload: unknown) => void) | undefined;
      reconnectedHandler = mittMocks.on.mock.calls.find(c => c[0] === 'ws:reconnected')?.[1] as
        (() => void) | undefined;
    }
  });

  it('registers the WS listeners exactly once (singleton guard)', () => {
    const callsBefore = mittMocks.on.mock.calls.length;
    useTodoList();
    useTodoList();
    expect(mittMocks.on.mock.calls.length).toBe(callsBefore);
    expect(mittMocks.on.mock.calls.filter(c => c[0] === 'ws:todo_updated')).toHaveLength(1);
    expect(mittMocks.on.mock.calls.filter(c => c[0] === 'ws:reconnected')).toHaveLength(1);
    expect(todoUpdatedHandler).toBeTypeOf('function');
    expect(reconnectedHandler).toBeTypeOf('function');
  });

  it('replaces todos wholesale on each ws:todo_updated frame', () => {
    const api = useTodoList();
    pushFrame([makeTodo('a')]);
    expect(api.todos.value.map(t => t.content)).toEqual(['a']);

    pushFrame([makeTodo('b'), makeTodo('c')]);
    expect(api.todos.value.map(t => t.content)).toEqual(['b', 'c']);
  });

  it('clears todos when the frame carries no todos array', () => {
    const api = useTodoList();
    pushFrame([makeTodo('a')]);
    todoUpdatedHandler!({ event: 'todo_updated', content: {} });
    expect(api.todos.value).toEqual([]);
    todoUpdatedHandler!({ event: 'todo_updated' });
    expect(api.todos.value).toEqual([]);
  });

  it('groups linked TaskFlow todos first, then unlinked, preserving order', () => {
    const api = useTodoList();
    pushFrame([
      makeTodo('u1'),
      makeTodo('f2a', { flow_id: 'flow-2', step_id: 'step-1' }),
      makeTodo('f1a', { flow_id: 'flow-1', step_id: 'step-1' }),
      makeTodo('f2b', { flow_id: 'flow-2', step_id: 'step-2' }),
      makeTodo('u2')
    ]);

    const groups = api.groups.value;
    expect(groups.map(g => g.flowId)).toEqual(['flow-2', 'flow-1', null]);
    expect(groups[0]!.items.map(t => t.content)).toEqual(['f2a', 'f2b']);
    expect(groups[1]!.items.map(t => t.content)).toEqual(['f1a']);
    expect(groups[2]!.items.map(t => t.content)).toEqual(['u1', 'u2']);
  });

  it('emits no unlinked group when all todos are flow-linked', () => {
    const api = useTodoList();
    pushFrame([makeTodo('f1', { flow_id: 'flow-1' })]);
    expect(api.groups.value.map(g => g.flowId)).toEqual(['flow-1']);
  });

  it('derives todoState hide -> close -> open and dockVisible accordingly', () => {
    const api = useTodoList();

    // empty -> hide
    expect(api.todoState.value).toBe('hide');
    expect(api.dockVisible.value).toBe(false);

    // all terminal -> close
    pushFrame([makeTodo('a', { status: 'completed' }), makeTodo('b', { status: 'cancelled' })]);
    expect(api.todoState.value).toBe('close');
    expect(api.dockVisible.value).toBe(false);

    // any non-terminal -> open
    pushFrame([makeTodo('a', { status: 'completed' }), makeTodo('b', { status: 'in_progress' })]);
    expect(api.todoState.value).toBe('open');
    expect(api.dockVisible.value).toBe(true);
  });

  it('counts completed and cancelled todos as done', () => {
    const api = useTodoList();
    pushFrame([
      makeTodo('a', { status: 'completed' }),
      makeTodo('b', { status: 'cancelled' }),
      makeTodo('c', { status: 'pending' }),
      makeTodo('d', { status: 'in_progress' })
    ]);
    expect(api.doneCount.value).toBe(2);
    expect(api.todos.value).toHaveLength(4);
  });

  it('refresh() sends a todo_refresh frame for the given session', () => {
    const api = useTodoList();
    api.refresh('sid-9');
    expect(mittMocks.emit).toHaveBeenCalledWith('ws:send', {
      event: 'todo_refresh',
      session_id: 'sid-9',
      content: ''
    });
  });

  it('init() pulls a snapshot for the session; a reconnect re-sends the refresh', () => {
    const api = useTodoList();
    api.init('sid-r');
    expect(mittMocks.emit).toHaveBeenCalledWith('ws:send', {
      event: 'todo_refresh',
      session_id: 'sid-r',
      content: ''
    });

    mittMocks.emit.mockClear();
    reconnectedHandler!();
    expect(mittMocks.emit).toHaveBeenCalledWith('ws:send', {
      event: 'todo_refresh',
      session_id: 'sid-r',
      content: ''
    });
  });

  it('exposes the persisted collapse flag and toggles it via the UI store', () => {
    const api = useTodoList();
    expect(api.collapsed.value).toBe(false);
    api.toggleCollapsed();
    expect(uiMock.todoDockCollapsed).toBe(true);
  });
});
