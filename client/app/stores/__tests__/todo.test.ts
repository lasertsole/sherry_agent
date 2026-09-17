import { describe, it, expect, vi, beforeEach } from 'vitest';
import { setActivePinia } from 'pinia';
import { createTestingPinia } from '@pinia/testing';
import type { Todo } from '../todo';
import { useTodoStore } from '../todo';

const mittMocks = vi.hoisted(() => ({ on: vi.fn(), emit: vi.fn(), off: vi.fn() }));

vi.mock('@/composables/mitt', () => mittMocks);

/**
 * Minimal valid todo fixture.
 * @param content Todo text.
 * @param overrides Fields overriding the defaults.
 * @returns A valid Todo object.
 */
function makeTodo(content: string, overrides: Partial<Todo> = {}): Todo {
  return { content, status: 'pending', priority: 'medium', ...overrides };
}

let store: ReturnType<typeof useTodoStore>;
let todoUpdatedHandler: ((payload: unknown) => void) | undefined;
let reconnectedHandler: (() => void) | undefined;

/**
 * Push a raw `todo_updated` frame into the captured handler.
 * @param contents
 */
function pushFrame(contents: Todo[]): void {
  todoUpdatedHandler!({ event: 'todo_updated', content: { todos: contents } });
}

describe('stores/todo', () => {
  beforeEach(() => {
    mittMocks.on.mockClear();
    mittMocks.emit.mockClear();
    setActivePinia(createTestingPinia({ stubActions: false }));
    store = useTodoStore();
    store.subscribe();
    todoUpdatedHandler = mittMocks.on.mock.calls.find(c => c[0] === 'ws:todo_updated')?.[1] as
      ((payload: unknown) => void) | undefined;
    reconnectedHandler = mittMocks.on.mock.calls.find(c => c[0] === 'ws:reconnected')?.[1] as (() => void) | undefined;
    window.history.replaceState({}, '', '/');
  });

  it('返回同一单例实例并暴露空默认值', () => {
    expect(useTodoStore()).toBe(store);
    expect(store.todos).toEqual([]);
    expect(store.currentSid).toBe('');
    expect(store.subscribed).toBe(true);
    expect(store.todoState).toBe('hide');
    expect(store.dockVisible).toBe(false);
  });

  it('registers the WS listeners exactly once (singleton guard)', () => {
    store.subscribe();
    store.subscribe();
    expect(mittMocks.on.mock.calls.filter(c => c[0] === 'ws:todo_updated')).toHaveLength(1);
    expect(mittMocks.on.mock.calls.filter(c => c[0] === 'ws:reconnected')).toHaveLength(1);
    expect(todoUpdatedHandler).toBeTypeOf('function');
    expect(reconnectedHandler).toBeTypeOf('function');
  });

  it('replaces todos wholesale on each ws:todo_updated frame', () => {
    pushFrame([makeTodo('a')]);
    expect(store.todos.map(t => t.content)).toEqual(['a']);

    pushFrame([makeTodo('b'), makeTodo('c')]);
    expect(store.todos.map(t => t.content)).toEqual(['b', 'c']);
  });

  it('clears todos when the frame carries no todos array', () => {
    pushFrame([makeTodo('a')]);
    todoUpdatedHandler!({ event: 'todo_updated', content: {} });
    expect(store.todos).toEqual([]);
    todoUpdatedHandler!({ event: 'todo_updated' });
    expect(store.todos).toEqual([]);
  });

  it('setTodos / setCurrentSid update state explicitly', () => {
    store.setTodos([makeTodo('x')]);
    store.setCurrentSid('sid-x');
    expect(store.todos.map(t => t.content)).toEqual(['x']);
    expect(store.currentSid).toBe('sid-x');
  });

  it('groups linked TaskFlow todos first, then unlinked, preserving order', () => {
    pushFrame([
      makeTodo('u1'),
      makeTodo('f2a', { flow_id: 'flow-2', step_id: 'step-1' }),
      makeTodo('f1a', { flow_id: 'flow-1', step_id: 'step-1' }),
      makeTodo('f2b', { flow_id: 'flow-2', step_id: 'step-2' }),
      makeTodo('u2')
    ]);

    const groups = store.groups;
    expect(groups.map(g => g.flowId)).toEqual(['flow-2', 'flow-1', null]);
    expect(groups[0]!.items.map(t => t.content)).toEqual(['f2a', 'f2b']);
    expect(groups[1]!.items.map(t => t.content)).toEqual(['f1a']);
    expect(groups[2]!.items.map(t => t.content)).toEqual(['u1', 'u2']);
  });

  it('emits no unlinked group when all todos are flow-linked', () => {
    pushFrame([makeTodo('f1', { flow_id: 'flow-1' })]);
    expect(store.groups.map(g => g.flowId)).toEqual(['flow-1']);
  });

  it('derives todoState hide -> close -> open and dockVisible accordingly', () => {
    expect(store.todoState).toBe('hide');
    expect(store.dockVisible).toBe(false);

    pushFrame([makeTodo('a', { status: 'completed' }), makeTodo('b', { status: 'cancelled' })]);
    expect(store.todoState).toBe('close');
    expect(store.dockVisible).toBe(false);

    pushFrame([makeTodo('a', { status: 'completed' }), makeTodo('b', { status: 'in_progress' })]);
    expect(store.todoState).toBe('open');
    expect(store.dockVisible).toBe(true);
  });

  it('counts completed and cancelled todos as done', () => {
    pushFrame([
      makeTodo('a', { status: 'completed' }),
      makeTodo('b', { status: 'cancelled' }),
      makeTodo('c', { status: 'pending' }),
      makeTodo('d', { status: 'in_progress' })
    ]);
    expect(store.doneCount).toBe(2);
    expect(store.todos).toHaveLength(4);
  });

  it('refreshTodos() sends a todo_refresh frame for the given session', () => {
    store.refreshTodos('sid-9');
    expect(mittMocks.emit).toHaveBeenCalledWith('ws:send', {
      event: 'todo_refresh',
      session_id: 'sid-9',
      content: ''
    });
    expect(store.currentSid).toBe('sid-9');
  });

  it('init() pulls a snapshot for the session; a reconnect re-sends the refresh', () => {
    store.init('sid-r');
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
});
