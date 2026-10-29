// `db` is a Nuxt auto-import (a composable export); only the type is imported.
import type { CommandHistoryEntry } from './db';
import type { CommandEntry } from './use-command-registry';

/**
 * The command palette's search history (Dexie, capped).
 *
 * Every executed command is recorded so the empty-query state can offer the
 * last few things the user ran. Re-running a command moves it to the top instead
 * of appending a duplicate; the table is capped so it cannot grow without bound.
 */

/** Keep at most this many rows (oldest pruned). */
export const COMMAND_HISTORY_CAP = 50;

/**
 * Record one execution (best-effort: history must never break the command).
 * @param entry The command that ran.
 */
export async function recordCommandHistory(entry: CommandEntry): Promise<void> {
  try {
    const now = Date.now();
    // Same command again → one row, refreshed, at the top.
    const existing = await db.commandHistory.where('commandId').equals(entry.id).primaryKeys();
    if (existing.length) await db.commandHistory.bulkDelete(existing);
    await db.commandHistory.add({
      commandId: entry.id,
      label: entry.label,
      icon: entry.icon,
      executedAt: now
    });
    const count = await db.commandHistory.count();
    if (count > COMMAND_HISTORY_CAP) {
      const oldest = await db.commandHistory
        .orderBy('executedAt')
        .limit(count - COMMAND_HISTORY_CAP)
        .primaryKeys();
      if (oldest.length) await db.commandHistory.bulkDelete(oldest);
    }
  } catch {
    // IndexedDB unavailable (private mode / a test without it): skip silently.
  }
}

/**
 * The most recent commands, newest first.
 * @param limit How many rows to return.
 */
export async function recentCommandHistory(limit = 5): Promise<CommandHistoryEntry[]> {
  try {
    return await db.commandHistory.orderBy('executedAt').reverse().limit(limit).toArray();
  } catch {
    return [];
  }
}

/** Forget the history. */
export async function clearCommandHistory(): Promise<void> {
  try {
    await db.commandHistory.clear();
  } catch {
    // See recordCommandHistory.
  }
}
