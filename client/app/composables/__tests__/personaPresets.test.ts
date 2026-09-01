/**
 * Unit tests for the AI persona preset persistence layer (Task 1 products):
 * the Dexie db helpers in `../db` (real `HistoryDb` singleton, no Dexie mocks)
 * and the `usePersonaPresets` module-level singleton composable, plus a schema
 * upgrade regression proving the version(9) `personaPresets` append broke none
 * of the v1-v8 tables.
 *
 * `import 'fake-indexeddb/auto'` MUST stay the first import: it injects the
 * fake IndexedDB implementation into globalThis before any module below
 * evaluates (happy-dom has no IndexedDB, and `../db` creates its Dexie
 * singleton at module scope). Tests run against the REAL db.ts helpers —
 * only the IndexedDB backend itself is faked.
 *
 * Isolation: every test starts from a wiped database (`db.delete()` in the
 * file-level beforeEach). Dexie re-opens the deleted database lazily on the
 * next operation, recreating all tables at the current version with an empty
 * store and a reset auto-increment key generator. The `usePersonaPresets`
 * singleton refs are module-level (shared across all callers in this file),
 * so the composable suite also resets them in its own beforeEach.
 */
import 'fake-indexeddb/auto';
import { describe, it, expect, beforeEach } from 'vitest';
import Dexie from 'dexie';
import {
  db,
  listPersonaPresets,
  getPersonaPreset,
  findPersonaPresetByName,
  createPersonaPreset,
  updatePersonaPreset,
  deletePersonaPreset,
  cacheCharacter,
  readCachedCharacter,
  cacheSessionMeta,
  readCachedSessionMetaList
} from '../db';
import { usePersonaPresets } from '../usePersonaPresets';

/** Persona file contents keyed by the exact workspace basenames (v1 fixture). */
const baseContent = (): Record<string, string> => ({
  'AGENTS.md': 'agents-v1',
  'IDENTITY.md': 'identity-v1',
  'SOUL.md': 'soul-v1',
  'USER.md': 'user-v1'
});

// Wipe the whole database before every test so cases are isolated and
// order-independent (fresh tables + reset auto-increment). Dexie 4 marks an
// instance as deleted by `delete()` and refuses to AUTO-reopen it afterwards
// (operations would throw DatabaseClosedError), so explicitly reopen: this
// recreates every table at the current version, empty, on the fake IndexedDB.
beforeEach(async () => {
  await db.delete();
  await db.open();
});

describe('persona preset db helpers (real Dexie over fake-indexeddb)', () => {
  it('createPersonaPreset returns auto-increment ids and stores the trimmed name', async () => {
    const id1 = await createPersonaPreset('  First  ', baseContent());
    const id2 = await createPersonaPreset('Second', baseContent());
    expect(typeof id1).toBe('number');
    expect(typeof id2).toBe('number');
    expect(id2).toBeGreaterThan(id1);
    // Name is stored trimmed (original case kept), not with surrounding whitespace.
    const first = await getPersonaPreset(id1);
    expect(first?.name).toBe('First');
  });

  it('createPersonaPreset throws an Error containing "duplicate" on an existing name (case-insensitive + trim)', async () => {
    await createPersonaPreset('Alpha', baseContent());
    await expect(createPersonaPreset('ALPHA', baseContent())).rejects.toThrowError(/duplicate/);
    await expect(createPersonaPreset('  alpha  ', baseContent())).rejects.toThrowError(/duplicate/);
    // Neither failed attempt may have written a row.
    expect(await db.personaPresets.count()).toBe(1);
  });

  it('listPersonaPresets returns presets ordered by createdAt ascending', async () => {
    // Seed with explicit, distinct timestamps (Date.now() alone is not monotonic
    // across rapid calls, and real timers/sleeps are not allowed in tests).
    await db.personaPresets.bulkAdd([
      { name: 'newest', content: baseContent(), createdAt: 300, updatedAt: 300 },
      { name: 'oldest', content: baseContent(), createdAt: 100, updatedAt: 100 },
      { name: 'middle', content: baseContent(), createdAt: 200, updatedAt: 200 }
    ]);
    const list = await listPersonaPresets();
    expect(list.map(p => p.name)).toEqual(['oldest', 'middle', 'newest']);
  });

  it('findPersonaPresetByName matches the exact name', async () => {
    await createPersonaPreset('abc', baseContent());
    const hit = await findPersonaPresetByName('abc');
    expect(hit?.name).toBe('abc');
  });

  it('findPersonaPresetByName matches case-insensitively ("abc" finds "ABC")', async () => {
    await createPersonaPreset('ABC', baseContent());
    const hit = await findPersonaPresetByName('abc');
    expect(hit).toBeDefined();
    // The stored row keeps its original case.
    expect(hit?.name).toBe('ABC');
  });

  it('findPersonaPresetByName matches after trimming surrounding whitespace ("  abc  " finds "abc")', async () => {
    await createPersonaPreset('abc', baseContent());
    const hit = await findPersonaPresetByName('  abc  ');
    expect(hit).toBeDefined();
    expect(hit?.name).toBe('abc');
  });

  it('findPersonaPresetByName returns undefined on a miss', async () => {
    await createPersonaPreset('Something', baseContent());
    await expect(findPersonaPresetByName('nope')).resolves.toBeUndefined();
  });

  it('updatePersonaPreset overwrites content and updatedAt but never the name', async () => {
    const id = await createPersonaPreset('Editable', baseContent());
    // Backdate deterministically so the bumped updatedAt is provably different
    // even when create/update land within the same millisecond.
    const stale = 1_000;
    await db.personaPresets.update(id, { createdAt: stale, updatedAt: stale });

    const updated: Record<string, string> = {
      'AGENTS.md': 'agents-v2',
      'IDENTITY.md': 'identity-v2',
      'SOUL.md': 'soul-v2',
      'USER.md': 'user-v2'
    };
    await updatePersonaPreset(id, updated);

    const row = await getPersonaPreset(id);
    expect(row?.name).toBe('Editable');
    expect(row?.content).toEqual(updated);
    expect(row?.updatedAt).toBeGreaterThan(stale);
    expect(row?.createdAt).toBe(stale);
  });

  it('updatePersonaPreset does not create additional records (count unchanged)', async () => {
    const id = await createPersonaPreset('Singular', baseContent());
    await updatePersonaPreset(id, baseContent());
    expect(await db.personaPresets.count()).toBe(1);
    expect(await db.personaPresets.get(id)).toBeDefined();
  });

  it('deletePersonaPreset removes the record', async () => {
    const id = await createPersonaPreset('Doomed', baseContent());
    await deletePersonaPreset(id);
    expect(await getPersonaPreset(id)).toBeUndefined();
    expect(await db.personaPresets.count()).toBe(0);
  });

  it('deletePersonaPreset on a nonexistent id does not throw', async () => {
    await createPersonaPreset('Keeper', baseContent());
    await expect(deletePersonaPreset(99999)).resolves.toBeUndefined();
    // The unrelated record survives the no-op delete.
    expect(await listPersonaPresets()).toHaveLength(1);
  });

  it('roundtrips content whose keys are exactly the four workspace persona files', async () => {
    const content: Record<string, string> = {
      'AGENTS.md': 'tool rules text',
      'IDENTITY.md': 'identity text',
      'SOUL.md': 'soul text',
      'USER.md': 'user preferences text'
    };
    const id = await createPersonaPreset('RoundTrip', content);
    const stored = await getPersonaPreset(id);
    // Deep-equal roundtrip: every value read back exactly as written.
    expect(stored?.content).toEqual(content);
    // The key set is exactly the four basenames, spelled verbatim.
    expect(Object.keys(stored?.content ?? {}).sort()).toEqual(['AGENTS.md', 'IDENTITY.md', 'SOUL.md', 'USER.md']);
  });
});

describe('usePersonaPresets composable (shared singleton over real Dexie)', () => {
  // The composable keeps module-level singleton refs shared by every caller in
  // this file. After the db wipe above, settle a refresh against the empty DB
  // and clear the refs so state cannot leak between cases. presetsLoaded only
  // gates the one-time auto-refresh, which this awaited refresh already covers.
  beforeEach(async () => {
    const api = usePersonaPresets();
    await api.refresh();
    api.presets.value = [];
    api.loading.value = false;
  });

  it('create maps a duplicate name to { ok: false, reason: "duplicate" } without throwing', async () => {
    const api = usePersonaPresets();
    expect(await api.create('Alpha', baseContent())).toEqual({ ok: true, id: expect.any(Number) });

    // Trim + case-insensitive duplicate → mapped result, never a thrown error.
    const dup = await api.create('  aLpHa  ', baseContent());
    expect(dup).toEqual({ ok: false, reason: 'duplicate' });
    // The failed attempt did not add a list entry.
    expect(api.presets.value).toHaveLength(1);
  });

  it('create returns { ok: true, id } and refreshes the shared presets list with the new entry', async () => {
    const api = usePersonaPresets();
    const res = await api.create('Beta', baseContent());
    expect(res).toEqual({ ok: true, id: expect.any(Number) });
    expect(api.presets.value).toHaveLength(1);
    expect(api.presets.value[0]?.name).toBe('Beta');
    expect(api.presets.value[0]?.content).toEqual(baseContent());
  });

  it('update returns true and the shared list reflects the new content', async () => {
    const api = usePersonaPresets();
    await api.create('Gamma', baseContent());
    const created = api.presets.value.find(p => p.name === 'Gamma');
    if (!created?.id) {
      expect.unreachable('created preset should be present in the list with an id');
    }

    const updated: Record<string, string> = {
      'AGENTS.md': 'agents-v2',
      'IDENTITY.md': 'identity-v2',
      'SOUL.md': 'soul-v2',
      'USER.md': 'user-v2'
    };
    await expect(api.update(created.id, updated)).resolves.toBe(true);

    const entry = api.presets.value.find(p => p.name === 'Gamma');
    expect(entry?.content).toEqual(updated);
  });

  it('remove returns true and the entry disappears from the shared list', async () => {
    const api = usePersonaPresets();
    await api.create('Delta', baseContent());
    const created = api.presets.value.find(p => p.name === 'Delta');
    if (!created?.id) {
      expect.unreachable('created preset should be present in the list with an id');
    }

    await expect(api.remove(created.id)).resolves.toBe(true);
    expect(api.presets.value.find(p => p.name === 'Delta')).toBeUndefined();
    expect(api.presets.value).toHaveLength(0);
  });

  it('loading is true while a refresh is in flight and false after it settles', async () => {
    const api = usePersonaPresets();
    // refresh() sets loading synchronously before its first await, so the check
    // below observes the in-flight state without any timers.
    const inFlight = api.refresh();
    expect(api.loading.value).toBe(true);
    await inFlight;
    expect(api.loading.value).toBe(false);
  });
});

describe('schema upgrade regression (version(9) personaPresets append)', () => {
  it('opens the current schema with personaPresets present and every legacy table readable', async () => {
    await db.open();
    const tableNames = db.tables.map(t => t.name);
    for (const name of [
      'messages',
      'character',
      'sessions',
      'drafts',
      'background',
      'subagentRuns',
      'sessionTitles',
      'personaPresets'
    ]) {
      expect(tableNames).toContain(name);
    }

    // Existing tables stay readable/writable through their exported helpers.
    const char = { session_id: 'ses_A', userName: 'u', userAvatar: '', aiName: 'Sherry', aiAvatar: '' };
    await cacheCharacter(char);
    await expect(readCachedCharacter('ses_A')).resolves.toEqual(char);
    await cacheSessionMeta({ id: 'ses_A', title: 't', createTime: 'x', updatedAt: 1 });
    await expect(readCachedSessionMetaList()).resolves.toHaveLength(1);
    await expect(db.messages.count()).resolves.toBe(0);

    // personaPresets is usable end-to-end on the freshly opened schema.
    const id = await createPersonaPreset('AfterOpen', baseContent());
    await expect(listPersonaPresets()).resolves.toHaveLength(1);
    await expect(getPersonaPreset(id)).resolves.toMatchObject({ name: 'AfterOpen' });
  });

  it('upgrades a pre-v9 database in place without losing existing rows', async () => {
    // The file-level beforeEach left an empty version(9) database open; free
    // the database name so the throwaway legacy instance can create a v1 one.
    await db.delete();

    // Build a legacy version(1) database under the same name with a throwaway
    // instance: a v1-era message row predating the token columns added in v6.
    const legacy = new Dexie('ema-history-cache');
    legacy.version(1).stores({ messages: 'id, [session_id+turn_num], session_id' });
    try {
      await legacy.open();
      await legacy.table('messages').put({
        id: 7,
        turn_num: 1,
        session_id: 'ses_A',
        role: 'user',
        content: 'legacy row',
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
        reasoning_content: null
      });
    } finally {
      legacy.close();
    }

    // Opening the real singleton (declaring versions 1..9) must run the whole
    // upgrade chain over the legacy store without error.
    await db.open();

    const row = await db.messages.get(7);
    expect(row?.session_id).toBe('ses_A');
    expect(row?.content).toBe('legacy row');
    // The v6 upgrade normalizes the token/model columns the legacy row lacks.
    expect(row?.model_name).toBeNull();
    expect(row?.input_tokens).toBeNull();
    expect(row?.output_tokens).toBeNull();

    // The appended version(9) table exists and works on the upgraded store.
    expect(db.tables.map(t => t.name)).toContain('personaPresets');
    const id = await createPersonaPreset('OnUpgraded', baseContent());
    expect(await db.personaPresets.count()).toBe(1);
    await expect(getPersonaPreset(id)).resolves.toMatchObject({ name: 'OnUpgraded' });
  });
});
