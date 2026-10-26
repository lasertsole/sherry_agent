/**
 * Unit tests for the Dexie cache wrappers in `../db`, run against the REAL
 * `HistoryDb` singleton over fake-indexeddb (no Dexie mocks): only the
 * IndexedDB backend is faked, so every wrapper's dispatch, index choice,
 * ordering and poisoned-row guard is exercised for real — a regression in
 * `../db` itself fails here (the pre-rewrite version mocked the whole module
 * and could never fail on one).
 *
 * `character` is covered by character.db.test.ts; the persona preset helpers
 * by personaPresets.test.ts.
 *
 * `import 'fake-indexeddb/auto'` MUST stay the first import: it injects the
 * fake IndexedDB implementation into globalThis before any module below
 * evaluates (happy-dom has no IndexedDB, and `../db` creates its Dexie
 * singleton at module scope).
 *
 * Isolation: every test starts from a wiped database (`db.delete()` in the
 * file-level beforeEach). Dexie 4 marks an instance as deleted by `delete()`
 * and refuses to AUTO-reopen it afterwards, so explicitly reopen: this
 * recreates every table at the current version, empty, with a reset
 * auto-increment key generator.
 */
import 'fake-indexeddb/auto';
import { describe, it, expect, beforeEach } from 'vitest';
import type { CachedMessage, CachedSessionMeta, DraftTurn, CachedSubagentRun } from '../db';
import {
  db,
  GLOBAL_SESSION_KEY,
  cacheMessages,
  readCachedMessages,
  cachedMaxTurnNum,
  clearCachedSession,
  cacheSessionMeta,
  readCachedSessionMetaList,
  clearCachedSessionMeta,
  saveDraftTurn,
  readDraftTurns,
  clearDraftTurn,
  clearDraftSession,
  saveBackground,
  readBackgroundConfig,
  cacheSubagentRuns,
  readCachedSubagentRuns,
  deleteCachedSubagentRuns,
  clearCachedSubagentRuns
} from '../db';
import { CHAT_ROLE, type MessageItem } from '@/pages/home/type';

const msg = (over: Partial<CachedMessage> = {}): CachedMessage => ({
  id: 1,
  turn_num: 1,
  session_id: 'ses_A',
  role: 'user',
  content: 'hi',
  timestamp: null,
  images: null,
  audios: null,
  videos: null,
  tool_call_id: null,
  tool_calls: null,
  tool_status: null,
  tool_name: null,
  finish_reason: null,
  reasoning: null,
  reasoning_content: null,
  model_name: null,
  input_tokens: null,
  output_tokens: null,
  ...over
});

/**
 * MessageItem-shaped fixture for DraftTurn.messages (role must be a real CHAT_ROLE value).
 * @param over
 */
const msgItem = (over: Partial<MessageItem> = {}): MessageItem => ({
  session_id: 'ses_A',
  role: CHAT_ROLE.USER,
  content: 'hi',
  id: 1,
  turn_num: 1,
  timestamp: '2026-01-01T00:00:00Z',
  ...over
});

const run = (over: Partial<CachedSubagentRun> = {}): CachedSubagentRun => ({
  run_id: '1',
  child_session_key: 'ses_child',
  requester_session_key: 'ses_A',
  task: 'task',
  task_name: 'task_name',
  label: null,
  agent_id: null,
  depth: 1,
  role: null,
  control_scope: null,
  generation: null,
  swarm_group_id: null,
  swarm_run_state: null,
  ended_reason: null,
  pause_reason: null,
  execution: { status: 'RUNNING', outcome: null, started_at: null, completed_at: null },
  completion: null,
  delivery: null,
  ...over
});

beforeEach(async () => {
  await db.delete();
  await db.open();
});

describe('cacheMessages', () => {
  it('no-ops on an empty array', async () => {
    await cacheMessages([]);
    expect(await db.messages.count()).toBe(0);
  });

  it('upserts rows deduplicated by id (a re-cached id overwrites)', async () => {
    await cacheMessages([msg({ id: 1, content: 'v1' }), msg({ id: 2, turn_num: 2 })]);
    await cacheMessages([msg({ id: 1, content: 'v2' })]);

    expect(await db.messages.count()).toBe(2);
    await expect(db.messages.get(1)).resolves.toMatchObject({ id: 1, content: 'v2' });
  });
});

describe('readCachedMessages', () => {
  it('returns only the session rows, ordered by turn_num ascending', async () => {
    // Seeded out of order and mixed with another session's rows: the compound
    // [session_id+turn_num] index must scope AND order the result for real.
    await cacheMessages([
      msg({ id: 1, turn_num: 3 }),
      msg({ id: 2, turn_num: 1 }),
      msg({ id: 3, turn_num: 2 }),
      msg({ id: 4, session_id: 'ses_B', turn_num: 0 })
    ]);

    await expect(readCachedMessages('ses_A')).resolves.toEqual([
      expect.objectContaining({ id: 2 }),
      expect.objectContaining({ id: 3 }),
      expect.objectContaining({ id: 1 })
    ]);
  });

  it('drops rows with a non-numeric turn_num (poisoned rows must not reach sorters)', async () => {
    // IndexedDB rejects NaN keys outright, so a poisoned row can only carry a
    // STRING turn_num (written by an older buggy build) — which sorts AFTER all
    // numbers and lands at the end of the index.
    await db.messages.bulkPut([
      msg({ id: 1, turn_num: 1 }),
      msg({ id: 2, turn_num: 'corrupt' as unknown as number }),
      msg({ id: 3, turn_num: 2 })
    ]);

    // Only the finite-number rows survive: one garbage turn_num makes
    // `(a.turn_num - b.turn_num)` NaN and destabilizes the whole sort.
    await expect(readCachedMessages('ses_A')).resolves.toEqual([
      expect.objectContaining({ id: 1 }),
      expect.objectContaining({ id: 3 })
    ]);
  });
});

describe('cachedMaxTurnNum', () => {
  it('returns the max turn_num when the cache is non-empty', async () => {
    await cacheMessages([msg({ id: 1, turn_num: 5 }), msg({ id: 2, turn_num: 2 })]);
    await expect(cachedMaxTurnNum('ses_A')).resolves.toBe(5);
  });

  it('returns 0 when the session has no cached rows', async () => {
    await expect(cachedMaxTurnNum('ses_A')).resolves.toBe(0);
  });

  it('falls back to the max over valid rows when the last row is poisoned', async () => {
    // Strings sort after all numbers in IndexedDB, so the poisoned row lands
    // last; the numeric max must come from a scan over valid rows instead.
    await db.messages.bulkPut([
      msg({ id: 1, turn_num: 3 }),
      msg({ id: 2, turn_num: 'corrupt' as unknown as number })
    ]);
    await expect(cachedMaxTurnNum('ses_A')).resolves.toBe(3);
  });

  it('returns 0 when every cached row is poisoned (forces a full refetch)', async () => {
    await db.messages.bulkPut([msg({ id: 1, turn_num: 'corrupt' as unknown as number })]);
    await expect(cachedMaxTurnNum('ses_A')).resolves.toBe(0);
  });
});

describe('clearCachedSession', () => {
  it('deletes only the given session messages, another session survives', async () => {
    await cacheMessages([
      msg({ id: 1 }),
      msg({ id: 2, turn_num: 2 }),
      msg({ id: 3, session_id: 'ses_B' })
    ]);
    await clearCachedSession('ses_A');

    await expect(readCachedMessages('ses_A')).resolves.toEqual([]);
    await expect(readCachedMessages('ses_B')).resolves.toHaveLength(1);
  });
});

describe('session meta (cacheSessionMeta / readCachedSessionMetaList / clearCachedSessionMeta)', () => {
  it('writes a placeholder that reads back by id', async () => {
    const meta: CachedSessionMeta = { id: 'ses_NEW', title: '新建对话', createTime: 'x', updatedAt: 2 };
    await cacheSessionMeta(meta);
    await expect(db.sessions.get('ses_NEW')).resolves.toEqual(meta);
  });

  it('reads all sessions sorted by updatedAt descending (newest first)', async () => {
    await cacheSessionMeta({ id: 'a', title: 'a', createTime: 'x', updatedAt: 1 });
    await cacheSessionMeta({ id: 'b', title: 'b', createTime: 'x', updatedAt: 3 });
    await cacheSessionMeta({ id: 'c', title: 'c', createTime: 'x', updatedAt: 2 });

    await expect(readCachedSessionMetaList()).resolves.toEqual([
      expect.objectContaining({ id: 'b' }),
      expect.objectContaining({ id: 'c' }),
      expect.objectContaining({ id: 'a' })
    ]);
  });

  it('deletes a placeholder by id, leaving the others', async () => {
    await cacheSessionMeta({ id: 'a', title: 'a', createTime: 'x', updatedAt: 1 });
    await cacheSessionMeta({ id: 'b', title: 'b', createTime: 'x', updatedAt: 2 });
    await clearCachedSessionMeta('a');

    const rest = await readCachedSessionMetaList();
    expect(rest.map(m => m.id)).toEqual(['b']);
  });
});

describe('drafts (saveDraftTurn / readDraftTurns / clearDraftTurn / clearDraftSession)', () => {
  it('writes whole-turn drafts and reads them back ordered by turn_num', async () => {
    await saveDraftTurn({ session_id: 'ses_A', turn_num: 2, messages: [msgItem({ id: -2, turn_num: 2 })] });
    await saveDraftTurn({ session_id: 'ses_A', turn_num: 1, messages: [msgItem({ id: -1, turn_num: 1 })] });

    const drafts = await readDraftTurns('ses_A');
    expect(drafts.map(d => d.turn_num)).toEqual([1, 2]);
    expect(drafts[0]!.messages[0]).toMatchObject({ id: -1, role: CHAT_ROLE.USER });
  });

  it('deletes a single draft by the [session_id+turn_num] composite key', async () => {
    await saveDraftTurn({ session_id: 'ses_A', turn_num: 1, messages: [msgItem()] });
    await saveDraftTurn({ session_id: 'ses_A', turn_num: 2, messages: [msgItem({ id: -2, turn_num: 2 })] });
    await clearDraftTurn('ses_A', 1);

    const drafts = await readDraftTurns('ses_A');
    expect(drafts.map(d => d.turn_num)).toEqual([2]);
  });

  it('clears every draft of a session, another session survives', async () => {
    await saveDraftTurn({ session_id: 'ses_A', turn_num: 1, messages: [msgItem()] });
    await saveDraftTurn({ session_id: 'ses_B', turn_num: 1, messages: [msgItem({ session_id: 'ses_B' })] });
    await clearDraftSession('ses_A');

    await expect(readDraftTurns('ses_A')).resolves.toEqual([]);
    await expect(readDraftTurns('ses_B')).resolves.toHaveLength(1);
  });
});

describe('background (saveBackground / readBackgroundConfig)', () => {
  it('round-trips the global background row under GLOBAL_SESSION_KEY', async () => {
    await saveBackground('data:image/png;base64,ZZZ', 55);

    await expect(readBackgroundConfig()).resolves.toEqual({
      backgroundUrl: 'data:image/png;base64,ZZZ',
      backgroundOpacity: 55
    });
    // The row really is the single global one, not a per-session copy.
    await expect(db.background.count()).resolves.toBe(1);
  });

  it('resolves to undefined when no row exists', async () => {
    await expect(readBackgroundConfig()).resolves.toBeUndefined();
  });

  it('treats a row with an empty backgroundUrl as unset', async () => {
    await db.background.put({ session_id: GLOBAL_SESSION_KEY, backgroundUrl: '', backgroundOpacity: 0 });
    await expect(readBackgroundConfig()).resolves.toBeUndefined();
  });

  it('defaults opacity to 0 on a legacy row written without one', async () => {
    await db.background.put({
      session_id: GLOBAL_SESSION_KEY,
      backgroundUrl: 'data:image/png;base64,ZZZ',
      backgroundOpacity: undefined
    });
    await expect(readBackgroundConfig()).resolves.toEqual({
      backgroundUrl: 'data:image/png;base64,ZZZ',
      backgroundOpacity: 0
    });
  });
});

describe('subagentRuns (cache / read / delete / clear)', () => {
  it('cacheSubagentRuns no-ops on an empty list', async () => {
    await cacheSubagentRuns([]);
    expect(await db.subagentRuns.count()).toBe(0);
  });

  it('cacheSubagentRuns upserts records keyed by run_id', async () => {
    await cacheSubagentRuns([run({ run_id: '1' })]);
    await cacheSubagentRuns([run({ run_id: '1', task: 'updated' }), run({ run_id: '2' })]);

    expect(await db.subagentRuns.count()).toBe(2);
    await expect(db.subagentRuns.get('1')).resolves.toMatchObject({ run_id: '1', task: 'updated' });
  });

  it('readCachedSubagentRuns sorts newest run_id first (numeric, not lexicographic)', async () => {
    await cacheSubagentRuns([run({ run_id: '1' }), run({ run_id: '3' }), run({ run_id: '10' })]);
    await expect(readCachedSubagentRuns()).resolves.toEqual([
      expect.objectContaining({ run_id: '10' }),
      expect.objectContaining({ run_id: '3' }),
      expect.objectContaining({ run_id: '1' })
    ]);
  });

  it('deleteCachedSubagentRuns no-ops on an empty list', async () => {
    await deleteCachedSubagentRuns([]);
    expect(await db.subagentRuns.count()).toBe(0);
  });

  it('deleteCachedSubagentRuns removes only the given run_ids', async () => {
    await cacheSubagentRuns([run({ run_id: '1' }), run({ run_id: '2' }), run({ run_id: '3' })]);
    await deleteCachedSubagentRuns(['1', '3']);

    const rest = await readCachedSubagentRuns();
    expect(rest.map(r => r.run_id)).toEqual(['2']);
  });

  it('clearCachedSubagentRuns empties the whole table', async () => {
    await cacheSubagentRuns([run({ run_id: '1' })]);
    await clearCachedSubagentRuns();
    expect(await db.subagentRuns.count()).toBe(0);
  });
});
