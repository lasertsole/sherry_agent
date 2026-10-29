/**
 * The command palette's search history (unit, real Dexie over fake-indexeddb).
 *
 * Contract: one row per command id (a rerun refreshes instead of duplicating),
 * newest first, capped at {@link COMMAND_HISTORY_CAP}, and every helper fails
 * open — a broken IndexedDB must never break the palette.
 *
 * `import 'fake-indexeddb/auto'` MUST stay first: `../db` creates its Dexie
 * singleton at module scope and happy-dom has no IndexedDB.
 */
import 'fake-indexeddb/auto';
import { describe, it, expect, beforeEach } from 'vitest';
import { db } from '../db';
import {
  COMMAND_HISTORY_CAP,
  clearCommandHistory,
  recentCommandHistory,
  recordCommandHistory
} from '../use-command-history';
import type { CommandEntry } from '../use-command-registry';

/**
 * An inert command.
 * @param overrides Fields to override.
 */
function command(overrides: Partial<CommandEntry> = {}): CommandEntry {
  return {
    id: 'toolbar.skills',
    label: '技能',
    section: 'tools',
    icon: 'pi pi-bolt',
    handler: () => undefined,
    ...overrides
  };
}

describe('use-command-history', () => {
  beforeEach(async () => {
    await db.delete();
    await db.open();
  });

  it('records a command and reads it back as the most recent', async () => {
    await recordCommandHistory(command());

    const rows = await recentCommandHistory();
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({ commandId: 'toolbar.skills', label: '技能', icon: 'pi pi-bolt' });
  });

  it('re-running a command keeps ONE row and refreshes its label', async () => {
    await recordCommandHistory(command({ label: '旧名' }));
    await recordCommandHistory(command({ label: '新名' }));

    const rows = await recentCommandHistory();
    expect(rows).toHaveLength(1);
    expect(rows[0]!.label).toBe('新名');
  });

  it('returns the newest rows first', async () => {
    await recordCommandHistory(command({ id: 'a', label: 'A' }));
    // Separate the timestamps explicitly: Date.now() can collide inside one ms.
    await db.commandHistory.where('commandId').equals('a').modify({ executedAt: 1_000 });
    await recordCommandHistory(command({ id: 'b', label: 'B' }));
    await db.commandHistory.where('commandId').equals('b').modify({ executedAt: 2_000 });

    expect((await recentCommandHistory()).map(row => row.commandId)).toEqual(['b', 'a']);
  });

  it('honours the limit', async () => {
    for (const id of ['a', 'b', 'c']) await recordCommandHistory(command({ id, label: id }));
    expect(await recentCommandHistory(2)).toHaveLength(2);
  });

  it('prunes the oldest rows past the cap', async () => {
    for (let index = 0; index < COMMAND_HISTORY_CAP + 3; index += 1) {
      await recordCommandHistory(command({ id: `cmd-${index}`, label: `cmd-${index}` }));
      // A unique, increasing timestamp per row: Date.now() ties would make the
      // "oldest" set depend on primary-key order instead of the index.
      await db.commandHistory
        .where('commandId')
        .equals(`cmd-${index}`)
        .modify({ executedAt: 1_000 + index });
    }

    expect(await db.commandHistory.count()).toBe(COMMAND_HISTORY_CAP);
    // The three oldest are gone, the newest survived.
    expect(await db.commandHistory.where('commandId').equals('cmd-0').count()).toBe(0);
    expect(
      await db.commandHistory
        .where('commandId')
        .equals(`cmd-${COMMAND_HISTORY_CAP + 2}`)
        .count()
    ).toBe(1);
  });

  it('clears every row', async () => {
    await recordCommandHistory(command());
    await clearCommandHistory();

    expect(await recentCommandHistory()).toEqual([]);
  });
});
