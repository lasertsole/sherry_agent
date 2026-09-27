import { describe, it, expect, beforeEach } from 'vitest';
import { setActivePinia } from 'pinia';
import { createTestingPinia } from '@pinia/testing';
import { useRunningCommandsStore } from '../running-commands';
import { CHAT_ROLE } from '~/types/chat-role';
import type { MessageItem } from '~/pages/home/type';

/**
 * One TOOL row, running unless told otherwise.
 * @param id
 * @param extra
 */
const toolRow = (id: number, extra: Partial<MessageItem> = {}): MessageItem =>
  ({
    id,
    session_id: 'sid-1',
    role: CHAT_ROLE.TOOL,
    content: '',
    toolName: 'terminal',
    toolStatus: 'running',
    toolArgs: { command: 'npm run build' },
    timestamp: '20260927213000',
    turn_num: 1,
    ...extra
  }) as MessageItem;

describe('stores/running-commands', () => {
  beforeEach(() => {
    setActivePinia(createTestingPinia({ stubActions: false }));
  });

  it('reports a running tool row while the session is generating', () => {
    const store = useRunningCommandsStore();

    store.sync([toolRow(7)], true);

    expect(store.commands).toHaveLength(1);
    expect(store.commands[0]).toMatchObject({ id: 7, toolName: 'terminal', summary: 'npm run build' });
    expect(store.commands[0]!.startedAtMs).toBe(new Date(2026, 8, 27, 21, 30, 0).getTime());
  });

  it('reads the command from the terminal tool\u2019s own argument shape', () => {
    const store = useRunningCommandsStore();

    store.sync(
      [
        // The terminal tool takes `commands` (string or list); a streamed row
        // carries an ISO timestamp rather than the persisted compact form.
        toolRow(1, { toolArgs: { commands: 'sleep 20 && echo done' } }),
        toolRow(2, { toolArgs: { commands: ['cd /tmp', 'ls -la'] }, timestamp: new Date().toISOString() })
      ],
      true
    );

    expect(store.commands.map(c => c.summary)).toEqual(['sleep 20 && echo done', 'cd /tmp && ls -la']);
    expect(store.commands[1]!.startedAtMs).toBeGreaterThan(Date.now() - 5000);
  });

  it('reports nothing once the session stops generating', () => {
    const store = useRunningCommandsStore();
    store.sync([toolRow(7)], true);
    expect(store.commands).toHaveLength(1);

    // The same rows, but the turn is over: whatever a row still says, a finished
    // turn owns no executing command.
    store.sync([toolRow(7)], false);

    expect(store.commands).toEqual([]);
  });

  it('ignores finished tool rows and non-tool rows', () => {
    const store = useRunningCommandsStore();

    store.sync(
      [
        toolRow(1, { toolStatus: 'done' }),
        toolRow(2, { toolStatus: 'failed' }),
        toolRow(3, { toolStatus: 'error' }),
        { ...toolRow(4), role: CHAT_ROLE.AI } as MessageItem
      ],
      true
    );

    expect(store.commands).toEqual([]);
  });

  it('summarizes from the command argument, falling back to the first arg', () => {
    const store = useRunningCommandsStore();

    store.sync(
      [
        toolRow(1, { toolName: 'python_repl', toolArgs: { code: 'print(1)\nprint(2)' } }),
        toolRow(2, { toolName: 'write_file', toolArgs: { file_path: 'a.md', text: 'hello' } }),
        toolRow(3, { toolName: 'terminal', toolArgs: {} })
      ],
      true
    );

    expect(store.commands.map(c => c.summary)).toEqual(['print(1)', 'file_path: a.md', 'terminal']);
  });
});
