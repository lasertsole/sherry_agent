import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { CachedMessage, CachedSessionMeta, DraftTurn, CachedSubagentRun, CachedBackground } from '../db';
import { CHAT_ROLE, type MessageItem } from '@/pages/home/type';

/** `where(...).between(...)` chain result exposed by the messages-table mock (only what ../db calls). */
interface MessagesBetweenResult {
  toArray: () => Promise<CachedMessage[]>;
  last: () => Promise<CachedMessage | undefined>;
}

/** `where(...)` clause shape exposed by the messages-table mock. */
interface MessagesWhereResult {
  between: (lower: [string, number], upper: [string, number]) => MessagesBetweenResult;
  equals: (key: string) => { delete: () => Promise<number> };
  delete: () => Promise<number>;
}

/** `where(...).between(...)` chain result exposed by the drafts-table mock. */
interface DraftsBetweenResult {
  toArray: () => Promise<DraftTurn[]>;
}

/** `where(...)` clause shape exposed by the drafts-table mock. */
interface DraftsWhereResult {
  between: (lower: [string, number], upper: [string, number]) => DraftsBetweenResult;
  equals: (key: string) => { delete: () => Promise<number> };
}

// Mock every table the `../db` wrappers touch (messages, sessions, drafts,
// background, subagentRuns) at the collection level. `character` is already
// covered by character.db.test.ts. Each table exposes the chain methods
// (where/between/last/delete/put/bulkPut/bulkDelete/toArray/clear) with
// explicitly typed vi.fn() declarations (matching the real Dexie chain
// signatures db.ts calls) so the wrapper dispatch is verifiable.
const messagesTable = vi.hoisted(() => {
  const between = vi.fn<(lower: [string, number], upper: [string, number]) => MessagesBetweenResult>();
  between.mockReturnValue({
    toArray: vi.fn<() => Promise<CachedMessage[]>>(async () => []),
    last: vi.fn<() => Promise<CachedMessage | undefined>>(async () => undefined)
  });
  const equals = vi.fn<(key: string) => { delete: () => Promise<number> }>();
  equals.mockReturnValue({ delete: vi.fn(async () => 0) });
  const where = vi.fn<(index: string) => MessagesWhereResult>();
  where.mockReturnValue({
    between,
    equals,
    delete: vi.fn(async () => 0)
  });
  return {
    bulkPut: vi.fn<(rows: CachedMessage[]) => Promise<void>>(),
    where,
    between
  };
});

const sessionsTable = vi.hoisted(() => ({
  put: vi.fn<(meta: CachedSessionMeta) => Promise<void>>(),
  toArray: vi.fn<() => Promise<CachedSessionMeta[]>>(),
  delete: vi.fn<(id: string) => Promise<void>>()
}));

const draftsTable = vi.hoisted(() => {
  const between = vi.fn<(lower: [string, number], upper: [string, number]) => DraftsBetweenResult>();
  between.mockReturnValue({ toArray: vi.fn<() => Promise<DraftTurn[]>>(async () => []) });
  const equals = vi.fn<(key: string) => { delete: () => Promise<number> }>();
  equals.mockReturnValue({ delete: vi.fn(async () => 0) });
  const where = vi.fn<(index: string) => DraftsWhereResult>();
  where.mockReturnValue({ between, equals });
  return {
    put: vi.fn<(draft: DraftTurn) => Promise<void>>(),
    delete: vi.fn<(key: [string, number]) => Promise<void>>(),
    where,
    between
  };
});

const backgroundTable = vi.hoisted(() => ({
  put: vi.fn<(row: CachedBackground) => Promise<void>>(),
  get: vi.fn<(key: string) => Promise<CachedBackground | undefined>>()
}));

const subagentRunsTable = vi.hoisted(() => ({
  bulkPut: vi.fn<(runs: CachedSubagentRun[]) => Promise<void>>(),
  toArray: vi.fn<() => Promise<CachedSubagentRun[]>>(),
  bulkDelete: vi.fn<(ids: string[]) => Promise<void>>(),
  clear: vi.fn<() => Promise<void>>()
}));

vi.mock('../db', () => ({
  GLOBAL_SESSION_KEY: '__global__',
  MIN_KEY: -Infinity,
  MAX_KEY: Infinity,
  DEFAULT_CACHED_CHARACTER: {},
  db: {
    messages: messagesTable,
    sessions: sessionsTable,
    drafts: draftsTable,
    background: backgroundTable,
    subagentRuns: subagentRunsTable
  },
  cacheMessages: async (rows: CachedMessage[]) => {
    if (!rows || rows.length === 0) return;
    await messagesTable.bulkPut(rows);
  },
  readCachedMessages: async (sessionId: string) => {
    return messagesTable.where('_').between([sessionId, -Infinity], [sessionId, Infinity]).toArray();
  },
  cachedMaxTurnNum: async (sessionId: string) => {
    const last = await messagesTable.where('_').between([sessionId, -Infinity], [sessionId, Infinity]).last();
    return last ? last.turn_num : 0;
  },
  clearCachedSession: async (sessionId: string) => {
    await messagesTable.where('session_id').equals(sessionId).delete();
  },
  cacheSessionMeta: async (meta: CachedSessionMeta) => {
    await sessionsTable.put(meta);
  },
  readCachedSessionMetaList: async () => {
    const list = await sessionsTable.toArray();
    return list.sort((a, b) => b.updatedAt - a.updatedAt);
  },
  clearCachedSessionMeta: async (sessionId: string) => {
    await sessionsTable.delete(sessionId);
  },
  saveDraftTurn: async (draft: DraftTurn) => {
    await draftsTable.put(draft);
  },
  readDraftTurns: async (sessionId: string) => {
    return draftsTable.where('_').between([sessionId, -Infinity], [sessionId, Infinity]).toArray();
  },
  clearDraftTurn: async (sessionId: string, turnNum: number) => {
    await draftsTable.delete([sessionId, turnNum]);
  },
  clearDraftSession: async (sessionId: string) => {
    await draftsTable.where('session_id').equals(sessionId).delete();
  },
  saveBackground: async (backgroundUrl: string, backgroundOpacity: number) => {
    await backgroundTable.put({ session_id: '__global__', backgroundUrl, backgroundOpacity });
  },
  readBackgroundConfig: async () => {
    const row = await backgroundTable.get('__global__');
    if (!row?.backgroundUrl) return undefined;
    return { backgroundUrl: row.backgroundUrl, backgroundOpacity: row.backgroundOpacity ?? 0 };
  },
  cacheSubagentRuns: async (runs: CachedSubagentRun[]) => {
    if (!runs || runs.length === 0) return;
    await subagentRunsTable.bulkPut(runs);
  },
  readCachedSubagentRuns: async () => {
    const list = await subagentRunsTable.toArray();
    return [...list].sort((a, b) => Number(b.run_id) - Number(a.run_id));
  },
  deleteCachedSubagentRuns: async (runIds: string[]) => {
    if (!runIds || runIds.length === 0) return;
    await subagentRunsTable.bulkDelete(runIds);
  },
  clearCachedSubagentRuns: async () => {
    await subagentRunsTable.clear();
  },
  cacheCharacter: async () => undefined,
  readCachedCharacter: async () => undefined,
  clearCachedCharacter: async () => undefined
}));

import {
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

/** MessageItem-shaped fixture for DraftTurn.messages (role must be a real CHAT_ROLE value). */
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
  spawn_mode: null,
  context_mode: null,
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

beforeEach(() => {
  messagesTable.bulkPut.mockClear();
  messagesTable.where.mockClear();
  messagesTable.between.mockClear();
  sessionsTable.put.mockClear();
  sessionsTable.toArray.mockClear();
  sessionsTable.delete.mockClear();
  draftsTable.put.mockClear();
  draftsTable.delete.mockClear();
  draftsTable.where.mockClear();
  draftsTable.between.mockClear();
  backgroundTable.put.mockClear();
  backgroundTable.get.mockClear();
  backgroundTable.get.mockResolvedValue(undefined);
  subagentRunsTable.bulkPut.mockClear();
  subagentRunsTable.toArray.mockClear();
  subagentRunsTable.toArray.mockResolvedValue([]);
  subagentRunsTable.bulkDelete.mockClear();
  subagentRunsTable.clear.mockClear();
});

describe('cacheMessages', () => {
  it('no-ops on an empty array', async () => {
    await cacheMessages([]);
    expect(messagesTable.bulkPut).not.toHaveBeenCalled();
  });

  it('bulkPuts non-empty rows deduplicated by id on the backend shape', async () => {
    const rows = [msg({ id: 1 }), msg({ id: 2, turn_num: 2 })];
    await cacheMessages(rows);
    expect(messagesTable.bulkPut).toHaveBeenCalledWith(rows);
  });
});

describe('readCachedMessages', () => {
  it('queries the compound [session_id+turn_num] index between session bounds', async () => {
    const rows = [msg({ id: 1 }), msg({ id: 2, turn_num: 2 })];
    messagesTable.between.mockReturnValue({ toArray: vi.fn(async () => rows), last: vi.fn(async () => undefined) });

    await expect(readCachedMessages('ses_A')).resolves.toEqual(rows);
    expect(messagesTable.where).toHaveBeenCalledWith('_');
    expect(messagesTable.between).toHaveBeenCalledWith(['ses_A', -Infinity], ['ses_A', Infinity]);
  });
});

describe('cachedMaxTurnNum', () => {
  it('returns the last row turn_num when cache is non-empty', async () => {
    messagesTable.between.mockReturnValue({
      toArray: vi.fn(async () => []),
      last: vi.fn(async () => msg({ turn_num: 5 }))
    });
    await expect(cachedMaxTurnNum('ses_A')).resolves.toBe(5);
  });

  it('returns 0 when the session has no cached rows', async () => {
    messagesTable.between.mockReturnValue({ toArray: vi.fn(async () => []), last: vi.fn(async () => undefined) });
    await expect(cachedMaxTurnNum('ses_A')).resolves.toBe(0);
  });
});

describe('clearCachedSession', () => {
  it('deletes all messages of a session by its session_id column', async () => {
    const del = vi.fn(async () => 0);
    messagesTable.where.mockReturnValue({
      between: vi.fn<MessagesWhereResult['between']>(),
      equals: vi.fn(() => ({ delete: del })),
      delete: vi.fn(async () => 0)
    });
    await clearCachedSession('ses_A');
    expect(messagesTable.where).toHaveBeenCalledWith('session_id');
    expect(del).toHaveBeenCalledTimes(1);
  });
});

describe('session meta (cacheSessionMeta / readCachedSessionMetaList / clearCachedSessionMeta)', () => {
  it('writes a placeholder via sessions.put', async () => {
    const meta: CachedSessionMeta = { id: 'ses_NEW', title: '新建对话', createTime: 'x', updatedAt: 2 };
    await cacheSessionMeta(meta);
    expect(sessionsTable.put).toHaveBeenCalledWith(meta);
  });

  it('reads all sessions sorted by updatedAt descending (newest first)', async () => {
    sessionsTable.toArray.mockResolvedValue([
      { id: 'a', title: 'a', createTime: 'x', updatedAt: 1 },
      { id: 'b', title: 'b', createTime: 'x', updatedAt: 3 },
      { id: 'c', title: 'c', createTime: 'x', updatedAt: 2 }
    ]);
    await expect(readCachedSessionMetaList()).resolves.toEqual([
      expect.objectContaining({ id: 'b' }),
      expect.objectContaining({ id: 'c' }),
      expect.objectContaining({ id: 'a' })
    ]);
  });

  it('deletes a placeholder by id via sessions.delete', async () => {
    await clearCachedSessionMeta('ses_NEW');
    expect(sessionsTable.delete).toHaveBeenCalledWith('ses_NEW');
  });
});

describe('drafts (saveDraftTurn / readDraftTurns / clearDraftTurn / clearDraftSession)', () => {
  it('writes a whole-turn draft via drafts.put', async () => {
    const draft: DraftTurn = { session_id: 'ses_A', turn_num: 1, messages: [msgItem()] };
    await saveDraftTurn(draft);
    expect(draftsTable.put).toHaveBeenCalledWith(draft);
  });

  it('reads drafts by compound key bounds', async () => {
    const draft: DraftTurn = { session_id: 'ses_A', turn_num: 1, messages: [msgItem()] };
    draftsTable.between.mockReturnValue({ toArray: vi.fn(async () => [draft]) });
    await expect(readDraftTurns('ses_A')).resolves.toEqual([draft]);
    expect(draftsTable.between).toHaveBeenCalledWith(['ses_A', -Infinity], ['ses_A', Infinity]);
  });

  it('deletes a single draft by the [session_id+turn_num] composite key', async () => {
    await clearDraftTurn('ses_A', 1);
    expect(draftsTable.delete).toHaveBeenCalledWith(['ses_A', 1]);
  });

  it('clears every draft of a session by its session_id column', async () => {
    const del = vi.fn(async () => 0);
    draftsTable.where.mockReturnValue({
      between: vi.fn<DraftsWhereResult['between']>(),
      equals: vi.fn(() => ({ delete: del }))
    });
    await clearDraftSession('ses_A');
    expect(draftsTable.where).toHaveBeenCalledWith('session_id');
    expect(del).toHaveBeenCalledTimes(1);
  });
});

describe('background (saveBackground / readBackgroundConfig)', () => {
  it('writes the global background row under GLOBAL_SESSION_KEY', async () => {
    await saveBackground('data:image/png;base64,ZZZ', 40);
    expect(backgroundTable.put).toHaveBeenCalledWith({
      session_id: GLOBAL_SESSION_KEY,
      backgroundUrl: 'data:image/png;base64,ZZZ',
      backgroundOpacity: 40
    });
  });

  it('defaults opacity to 0 when the row has no backgroundUrl (unset)', async () => {
    backgroundTable.get.mockResolvedValue(undefined);
    await expect(readBackgroundConfig()).resolves.toBeUndefined();
  });

  it('returns the full config when a backgroundUrl is set', async () => {
    backgroundTable.get.mockResolvedValue({
      session_id: GLOBAL_SESSION_KEY,
      backgroundUrl: 'data:image/png;base64,ZZZ',
      backgroundOpacity: 55
    });
    await expect(readBackgroundConfig()).resolves.toEqual({
      backgroundUrl: 'data:image/png;base64,ZZZ',
      backgroundOpacity: 55
    });
  });

  it('treats a row with an empty backgroundUrl as unset', async () => {
    backgroundTable.get.mockResolvedValue({
      session_id: GLOBAL_SESSION_KEY,
      backgroundUrl: '',
      backgroundOpacity: 0
    });
    await expect(readBackgroundConfig()).resolves.toBeUndefined();
  });
});

describe('subagentRuns (cache / read / delete / clear)', () => {
  it('cacheSubagentRuns no-ops on an empty list', async () => {
    await cacheSubagentRuns([]);
    expect(subagentRunsTable.bulkPut).not.toHaveBeenCalled();
  });

  it('cacheSubagentRuns bulkPuts records keyed by run_id', async () => {
    const runs = [run({ run_id: '1' }), run({ run_id: '2' })];
    await cacheSubagentRuns(runs);
    expect(subagentRunsTable.bulkPut).toHaveBeenCalledWith(runs);
  });

  it('readCachedSubagentRuns sorts newest run_id first', async () => {
    subagentRunsTable.toArray.mockResolvedValue([run({ run_id: '1' }), run({ run_id: '3' }), run({ run_id: '10' })]);
    const result = await readCachedSubagentRuns();
    expect(result.map(r => r.run_id)).toEqual(['10', '3', '1']);
  });

  it('deleteCachedSubagentRuns no-ops on an empty list', async () => {
    await deleteCachedSubagentRuns([]);
    expect(subagentRunsTable.bulkDelete).not.toHaveBeenCalled();
  });

  it('deleteCachedSubagentRuns bulkDeletes the given run_ids', async () => {
    await deleteCachedSubagentRuns(['1', '2', '3']);
    expect(subagentRunsTable.bulkDelete).toHaveBeenCalledWith(['1', '2', '3']);
  });

  it('clearCachedSubagentRuns empties the whole table', async () => {
    await clearCachedSubagentRuns();
    expect(subagentRunsTable.clear).toHaveBeenCalledTimes(1);
  });
});
