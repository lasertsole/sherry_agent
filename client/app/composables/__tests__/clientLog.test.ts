import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';

/**
 * In-memory persistence fake, injected via `setClientLogStore` so the composable
 * logic (capture -> buffer + persistence + live subscription, history read,
 * clear) runs without a real IndexedDB/Dexie.
 */
/** Local-day start-of-day (replicated here so the fake store does not depend on the module under test). */
function localDayStart(ts: number): number {
  const d = new Date(ts);
  d.setHours(0, 0, 0, 0);
  return d.getTime();
}

/** Local-date bucket name, e.g. `2026-08-17`. */
function localBucketName(ts: number): string {
  const d = new Date(ts);
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

/** In-memory row shape (type persisted for indexed-type queries; absent rows are legacy). */
interface Row {
  id?: number;
  level: string;
  type?: 'all' | 'log' | 'error';
  text: string;
  ts: number;
}

/** Replicate levelToType so the fake store matches the classifier without importing the module under test. */
function levelToTypeFor(level: string): 'all' | 'log' | 'error' {
  switch ((level || '').toUpperCase()) {
    case 'ERROR':
    case 'CRITICAL':
      return 'error';
    case 'INFO':
      return 'log';
    default:
      return 'all';
  }
}

/** Resolve a row's type: persisted field wins, otherwise derived from level (legacy compat). */
function typeOfRow(e: Row): 'all' | 'log' | 'error' {
  return e.type ?? levelToTypeFor(e.level);
}

function makeFakeStore() {
  const rows: Row[] = [];
  let nextId = 1;
  const DAY_MS = 24 * 60 * 60 * 1000;
  return {
    rows,
    add: vi.fn(async (entry: Row) => {
      rows.push({ ...entry, id: nextId });
      return nextId++;
    }),
    clear: vi.fn(async () => {
      rows.length = 0;
    }),
    readPage: vi.fn(async (opts: { limit: number; beforeId?: number }) => {
      let list = [...rows];
      if (opts.beforeId !== undefined) list = list.filter(e => (e.id ?? 0) < opts.beforeId!);
      // Newest-first (rows are stored oldest-first, so reverse then cap).
      return list.reverse().slice(0, opts.limit);
    }),
    listTypes: vi.fn(async () => {
      const byType = new Map<'all' | 'log' | 'error', { count: number; days: Set<number> }>();
      for (const e of rows) {
        const type = typeOfRow(e);
        const cur = byType.get(type) ?? { count: 0, days: new Set<number>() };
        cur.count += 1;
        cur.days.add(localDayStart(e.ts));
        byType.set(type, cur);
      }
      // Fixed UI order all/log/error.
      return (['all', 'log', 'error'] as const).map(type => {
        const v = byType.get(type);
        return { type, count: v?.count ?? 0, dayCount: v?.days.size ?? 0 };
      });
    }),
    listBucketsForType: vi.fn(async (type: 'all' | 'log' | 'error') => {
      const scoped = rows.filter(e => typeOfRow(e) === type);
      if (scoped.length === 0) return [];
      const byDay = new Map<number, { count: number; minTs: number }>();
      for (const e of scoped) {
        const start = localDayStart(e.ts);
        const cur = byDay.get(start);
        if (cur) {
          cur.count += 1;
          if (e.ts < cur.minTs) cur.minTs = e.ts;
        } else {
          byDay.set(start, { count: 1, minTs: e.ts });
        }
      }
      return [...byDay.entries()]
        .map(([start, v]) => ({
          type,
          name: localBucketName(v.minTs),
          tsStart: start,
          tsEnd: start + DAY_MS,
          count: v.count,
          is_current: start === localDayStart(Date.now())
        }))
        .sort((a, b) => b.tsStart - a.tsStart);
    }),
    readBucket: vi.fn(async (t: { type: 'all' | 'log' | 'error'; tsStart: number; tsEnd: number; limit?: number }) => {
      return [...rows]
        .filter(e => e.ts >= t.tsStart && e.ts < t.tsEnd)
        .filter(e => t.type === 'all' || typeOfRow(e) === t.type)
        .sort((a, b) => b.ts - a.ts)
        .slice(0, t.limit ?? 500);
    })
  };
}

// Module under test comes via dynamic import so each test gets a fresh module
// instance (`captureInstalled` resets, and the buffer / store injection is clean).
type ClientLogModule = typeof import('../clientLog');

describe('clientLog composable', () => {
  let mod: ClientLogModule;
  let store: ReturnType<typeof makeFakeStore>;

  const origConsole = {
    debug: console.debug,
    log: console.log,
    info: console.info,
    warn: console.warn,
    error: console.error
  } as const;

  function restoreConsole() {
    console.debug = origConsole.debug;
    console.log = origConsole.log;
    console.info = origConsole.info;
    console.warn = origConsole.warn;
    console.error = origConsole.error as typeof origConsole.error;
  }

  beforeEach(async () => {
    vi.resetModules();
    mod = await import('../clientLog');
    store = makeFakeStore();
    mod.setClientLogStore(store);
    restoreConsole();
  });

  afterEach(() => {
    restoreConsole();
    vi.restoreAllMocks();
  });

  describe('capture + persistence + live subscription', () => {
    it('patches console once, persists each call, and emits to subscribers', () => {
      mod.installClientLogCapture();
      mod.installClientLogCapture(); // idempotent — must not double-wrap

      const received: { level: string; text: string }[] = [];
      const off = mod.subscribeClientLogs(e => received.push(e));

      console.log('hello', { a: 1 });
      console.warn('careful');
      console.error('boom');

      expect(store.add).toHaveBeenCalledTimes(3);
      expect(received).toHaveLength(3);
      expect(received.map(e => e.level)).toEqual(['INFO', 'WARNING', 'ERROR']);
      expect(received[0]!.text).toContain('hello');
      expect(received[1]!.text).toContain('careful');
      expect(received[2]!.text).toContain('boom');

      off();
    });

    it('persists an inferred type on each captured entry (mirrors server info/all/error buckets)', () => {
      mod.installClientLogCapture();
      console.info('a'); // INFO -> log
      console.warn('b'); // WARNING -> all
      console.error('c'); // ERROR -> error

      const added = store.rows.map(r => ({ level: r.level, type: r.type }));
      expect(added).toEqual([
        { level: 'INFO', type: 'log' },
        { level: 'WARNING', type: 'all' },
        { level: 'ERROR', type: 'error' }
      ]);
    });

    it('debug maps to DEBUG and info maps to INFO', () => {
      mod.installClientLogCapture();
      const received: { level: string }[] = [];
      const off = mod.subscribeClientLogs(e => received.push(e));

      console.debug('detail');
      console.info('progress');

      expect(received.map(e => e.level)).toEqual(['DEBUG', 'INFO']);
      off();
    });
  });

  describe('history read', () => {
    it('delegates a page query to the active store', async () => {
      await mod.readClientLogs({ limit: 50 });
      expect(store.readPage).toHaveBeenCalledWith({ limit: 50 });
    });

    it('passes beforeId through for pagination', async () => {
      await mod.readClientLogs({ limit: 50, beforeId: 100 });
      expect(store.readPage).toHaveBeenCalledWith({ limit: 50, beforeId: 100 });
    });

    it('returns rows newest-first from a populated store', async () => {
      store.rows.push({ id: 1, level: 'INFO', text: 'old', ts: 1 });
      store.rows.push({ id: 2, level: 'ERROR', text: 'new', ts: 2 });
      const result = await mod.readClientLogs({ limit: 10 });
      expect(result.map(e => e.text)).toEqual(['new', 'old']);
    });

    it('filters rows by beforeId when provided', async () => {
      store.rows.push({ id: 1, level: 'INFO', text: 'oldest', ts: 1 });
      store.rows.push({ id: 2, level: 'INFO', text: 'middle', ts: 2 });
      store.rows.push({ id: 3, level: 'INFO', text: 'latest', ts: 3 });
      const result = await mod.readClientLogs({ limit: 10, beforeId: 3 });
      // Excludes id=3; newest-first of the remaining.
      expect(result.map(e => e.text)).toEqual(['middle', 'oldest']);
    });
  });

  describe('buffer + clear', () => {
    it('getLogBufferSnapshot reflects captured entries', () => {
      mod.installClientLogCapture();
      console.info('buffered-a');
      const snapshot = mod.getLogBufferSnapshot();
      expect(Array.isArray(snapshot)).toBe(true);
      expect(snapshot.some(e => e.text.includes('buffered-a'))).toBe(true);
    });

    it('clearClientLogs clears the store and the memory buffer', async () => {
      mod.installClientLogCapture();
      console.info('x');
      await mod.clearClientLogs();
      expect(store.clear).toHaveBeenCalled();
      expect(mod.getLogBufferSnapshot()).toEqual([]);
    });
  });

  describe('levelToType classifier (mirrors server info/all/error buckets)', () => {
    it('maps ERROR/CRITICAL to error, INFO to log, everything else to all', () => {
      expect(mod.levelToType('ERROR')).toBe('error');
      expect(mod.levelToType('Critical')).toBe('error');
      expect(mod.levelToType('INFO')).toBe('log');
      expect(mod.levelToType('TRACE')).toBe('all');
      expect(mod.levelToType('DEBUG')).toBe('all');
      expect(mod.levelToType('SUCCESS')).toBe('all');
      expect(mod.levelToType('WARNING')).toBe('all');
      expect(mod.levelToType('')).toBe('all');
      expect(mod.levelToType('UNKNOWN')).toBe('all');
    });

    it('typeOfEntry prefers persisted type, else infers from level (legacy rows)', () => {
      expect(mod.typeOfEntry({ level: 'ERROR', text: '', ts: 0 })).toBe('error');
      expect(mod.typeOfEntry({ level: 'INFO', text: '', ts: 0 })).toBe('log');
      expect(mod.typeOfEntry({ level: 'INFO', type: 'all', text: '', ts: 0 })).toBe('all');
    });
  });

  describe('type + date buckets (mirrors server info/all/error dirs + per-day files)', () => {
    // Computed at runtime (inside each `it`) because `mod` is assigned in `beforeEach`.
    function dayStarts() {
      const now = Date.now();
      const todayStart = mod.startOfLocalDay(now);
      const twoDaysAgo = new Date(todayStart);
      twoDaysAgo.setDate(twoDaysAgo.getDate() - 2);
      return { todayStart, twoDaysStart: twoDaysAgo.getTime() };
    }

    // Pushes rows with a mix of types across two days. Returns the pushed rows.
    function seedRows(todayStart: number, twoDaysStart: number) {
      const rows = [
        { id: 1, level: 'INFO', text: 'today-info', ts: todayStart + 1 }, // log | today
        { id: 2, level: 'ERROR', text: 'today-error', ts: todayStart + 2 }, // error | today
        { id: 3, level: 'DEBUG', text: 'today-debug', ts: todayStart + 3 }, // all | today
        { id: 4, level: 'INFO', text: 'older-info', ts: twoDaysStart + 1 }, // log | older
        { id: 5, level: 'ERROR', text: 'older-error', ts: twoDaysStart + 2 } // error | older
      ];
      for (const r of rows) store.rows.push(r);
      return rows;
    }

    it('listClientLogTypes aggregates counts by type (fixed all/log/error order)', async () => {
      const { todayStart, twoDaysStart } = dayStarts();
      seedRows(todayStart, twoDaysStart);

      const types = await mod.listClientLogTypes();
      expect(types).toHaveLength(3);
      expect(types.map(t => t.type)).toEqual(['all', 'log', 'error']);
      // all: DEBUG-only(2 days); log: INFO-only(2 days); error: ERROR-only(2 days).
      expect(types[0]).toEqual({ type: 'all', count: 1, dayCount: 1 });
      expect(types[1]).toEqual({ type: 'log', count: 2, dayCount: 2 });
      expect(types[2]).toEqual({ type: 'error', count: 2, dayCount: 2 });
    });

    it('listClientLogBucketsForType groups rows of that type by day, newest first, scoped to type', async () => {
      const { todayStart, twoDaysStart } = dayStarts();
      seedRows(todayStart, twoDaysStart);

      const errorBuckets = await mod.listClientLogBucketsForType('error');
      expect(errorBuckets).toHaveLength(2);
      expect(errorBuckets[0]!.type).toBe('error');
      expect(errorBuckets[0]!.tsStart).toBe(todayStart);
      expect(errorBuckets[0]!.count).toBe(1);
      expect(errorBuckets[0]!.is_current).toBe(true);
      expect(errorBuckets[1]!.type).toBe('error');
      expect(errorBuckets[1]!.tsStart).toBe(twoDaysStart);
      expect(errorBuckets[1]!.count).toBe(1);
      expect(errorBuckets[1]!.is_current).toBe(false);

      // 'log' type must not leak error rows into it.
      const logBuckets = await mod.listClientLogBucketsForType('log');
      const todayLog = logBuckets.find(b => b.is_current)!;
      expect(todayLog.count).toBe(1);
      expect(todayLog.name).toBe(mod.bucketNameOf(todayStart));
    });

    it('readClientLogBucket scopes rows to type and day, newest-first (all = unfiltered)', async () => {
      const { todayStart, twoDaysStart } = dayStarts();
      seedRows(todayStart, twoDaysStart);

      // error bucket today -> only the ERROR row.
      const errorBucket = (await mod.listClientLogBucketsForType('error')).find(b => b.is_current)!;
      const errorRows = await mod.readClientLogBucket(errorBucket, 10);
      expect(errorRows.map(e => e.text)).toEqual(['today-error']);

      // log bucket today -> only the INFO row.
      const logBucket = (await mod.listClientLogBucketsForType('log')).find(b => b.is_current)!;
      const logRows = await mod.readClientLogBucket(logBucket, 10);
      expect(logRows.map(e => e.text)).toEqual(['today-info']);

      // all bucket today -> all three (newest-first: debug > error > info).
      const allBucket = (await mod.listClientLogBucketsForType('all')).find(b => b.is_current)!;
      const allRows = await mod.readClientLogBucket(allBucket, 10);
      expect(allRows.map(e => e.text)).toEqual(['today-debug', 'today-error', 'today-info']);
    });

    it('typeOfEntry fallback supports legacy rows without a persisted type', async () => {
      const { todayStart } = dayStarts();
      // Legacy row WITHOUT type field (older entries written pre-type-refactor).
      store.rows.push({ id: 1, level: 'ERROR', text: 'legacy-error', ts: todayStart + 1 });

      const types = await mod.listClientLogTypes();
      expect(types.find(t => t.type === 'error')!.count).toBe(1);
      const errorBucket = (await mod.listClientLogBucketsForType('error')).find(b => b.is_current)!;
      const rows = await mod.readClientLogBucket(errorBucket, 10);
      expect(rows.map(e => e.text)).toEqual(['legacy-error']);
    });
  });
});
