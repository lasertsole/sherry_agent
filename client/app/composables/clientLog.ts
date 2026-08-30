import Dexie, { type Table } from 'dexie';

/**
 * Top-level type buckets for client logs (mirroring the `info`/`all`/`error` trio of directories in server logs/logger.py).
 * The order is the UI display order: `all` (everything), `log` (INFO stream), `error` (errors).
 */
export const CLIENT_LOG_TYPES = ['all', 'log', 'error'] as const;
export type ClientLogType = (typeof CLIENT_LOG_TYPES)[number];

/**
 * Persistence store abstraction: the default implementation targets Dexie (IndexedDB), so tests can inject an in-memory fake.
 *
 * Bucketing semantics: **first split into top-level buckets by type, then split each type into per-day sub-buckets (date)**,
 * mirroring the server's "directory (info/all/error) / per-day file" layout.
 */
export interface ClientLogStore {
  add(entry: ClientLogEntry): Promise<number>;
  clear(): Promise<void>;
  /** Read history (newest first). When `beforeId` is present, only records with `id < beforeId` are returned. */
  readPage(opts: { limit: number; beforeId?: number }): Promise<ClientLogEntry[]>;
  /** List top-level type bucket metadata (total entries, day counts), for the "bucket by type" dropdown — mirrors the server's three log directories. */
  listTypes(): Promise<ClientLogTypeInfo[]>;
  /** List the per-day sub-buckets inside a given type bucket (newest first), for the "bucket by day" dropdown. */
  listBucketsForType(type: ClientLogType): Promise<ClientLogBucket[]>;
  /** Read the logs inside one type's per-day bucket (newest first). `tsStart` is that day's local midnight, `tsEnd` is the next midnight (half-open interval). */
  readBucket(t: { type: ClientLogType; tsStart: number; tsEnd: number; limit?: number }): Promise<ClientLogEntry[]>;
}

/**
 * Frontend (client-side) log capture and persistence.
 *
 * Goal: give the "Log Viewer → Client" tab the same **history + live** capabilities as the "Server" tab.
 *
 * - **Live**: install the browser `console.*` capture once; each newly captured entry is pushed
 *   to {@link clientLogSubscribers} right away;
 * - **History**: every capture is written synchronously into the IndexedDB (Dexie) `client_logs`
 *   table; after an app restart the records are rebuilt via {@link readClientLogs}, achieving true
 *   persistence across sessions (unlike the old in-memory-only behavior);
 * - **Cleanup**: {@link clearClientLogs} empties both Dexie and the in-memory buffer so history
 *   does not grow unboundedly.
 *
 * Capture only applies to frontend Vue/browser-side `console.*` calls; Rust tracing (`src-tauri`) logs are not covered here.
 */

/** A single client log entry. */
export interface ClientLogEntry {
  /** Dexie auto-increment primary key, used for history paging (`beforeId`) and ordering. */
  id?: number;
  /** Log level (uppercase, matching the server tab's coloring rules). */
  level: string;
  /** Pre-formatted text (including the timestamp header `HH:MM:SS | LEVEL | message`). */
  text: string;
  /** Unix millisecond timestamp at capture time (used for time-based ordering). */
  ts: number;
  /**
   * The top-level type bucket (`all`/`log`/`error`) this entry belongs to, inferred by
   * {@link levelToType} at capture time and persisted for efficient type-based retrieval.
   * Historical data (missing this field) falls back to {@link typeOfEntry} inference at read time.
   */
  type?: ClientLogType;
}

/**
 * Map a log level to its top-level type bucket — mirrors the three-directory semantics of server logs/logger.py:
 * - `all`: every level; `log` (counterpart of the info directory) only INFO; `error`: ERROR + CRITICAL.
 * Client console capture actually only produces DEBUG/INFO/WARNING/ERROR, but TRACE/SUCCESS
 * and unknown levels are also handled so classification aligns exactly with the server.
 */
export function levelToType(level: string): ClientLogType {
  switch ((level || '').toUpperCase()) {
    case 'ERROR':
    case 'CRITICAL':
      return 'error';
    case 'INFO':
      return 'log';
    default:
      // TRACE / DEBUG / SUCCESS / WARNING / unknown levels all fall into `all` (same as the server's all bucket).
      return 'all';
  }
}

/** Get an entry's type: prefer the persisted field, falling back to level-based inference when missing (compatible with old historical data). */
export function typeOfEntry(entry: ClientLogEntry): ClientLogType {
  return entry.type ?? levelToType(entry.level);
}

/** Cap for the in-memory render buffer: oldest records are dropped beyond it. */
export const MAX_LOG_RECS = 5000;

/**
 * Top-level type bucket metadata — mirrors the `info`/`all`/`error` directories under server-side logs/output.
 * Used as the first level of the "bucket by type" dropdown.
 */
export interface ClientLogTypeInfo {
  /** Type identifier (`all`/`log`/`error`). */
  type: ClientLogType;
  /** Total number of log entries under this type. */
  count: number;
  /** Number of per-day sub-buckets (days) this type contains. */
  dayCount: number;
}

/**
 * Per-day sub-bucket metadata inside a given type bucket — mirrors the server-side `LogFileInfo` (per-day files).
 *
 * - Each client log entry is filed into its "current day" bucket at capture time, and at the same
 *   time into the type bucket inferred from its level;
 * - A day bucket corresponds to one "file" in the dropdown; today's day bucket is treated as the
 *   current one (is_current=true) and only it (with the matching type) can start a live stream
 *   (mirroring the server, where only the current process's logs can be streamed live).
 */
export interface ClientLogBucket {
  /** The top-level type bucket it belongs to. */
  type: ClientLogType;
  /** Display name of the bucket, e.g. `2026-08-17`. */
  name: string;
  /** Unix millisecond timestamp of that day's midnight (local timezone), the left endpoint of the read interval. */
  tsStart: number;
  /** Unix millisecond timestamp of next day's midnight (right endpoint of the half-open interval). */
  tsEnd: number;
  /** Number of log entries inside this bucket. */
  count: number;
  /** Whether this is "today's" bucket (the only one that can start a live stream). */
  is_current: boolean;
}

/** Local date bucketing granularity: one bucket per day. */
const DAY_MS = 24 * 60 * 60 * 1000;

/** Truncate a Unix millisecond timestamp to the local-timezone midnight of its day. */
export function startOfLocalDay(ts: number): number {
  const d = new Date(ts);
  d.setHours(0, 0, 0, 0);
  return d.getTime();
}

/** Compute the display name of the bucket a timestamp belongs to (local timezone, formatted as `YYYY-MM-DD`). */
export function bucketNameOf(ts: number): string {
  const d = new Date(ts);
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

class ClientLogDb extends Dexie {
  /** Client log table: auto-incrementing `id` primary key; each entry carries `ts` and `type` for history paging and type-based retrieval. */
  logs!: Table<ClientLogEntry, number>;

  constructor() {
    super('ema-client-log-cache');
    this.version(2).stores({
      // `ts` is indexed so history can be read in reverse time order; `type` + `type&ts` support aggregation by type bucket and range queries;
      // the auto-incrementing `id` primary key is naturally ordered. Old history has no `type` field; typeOfEntry infers it as a fallback at read time.
      logs: '++id, ts, type, [type+ts]'
    });
  }
}

/** The single global client log database instance. */
export const logDb = new ClientLogDb();

/** Default (production) persistence layer: forwards reads/writes to Dexie. */
const dexieStore: ClientLogStore = {
  async add(entry) {
    return await logDb.logs.add(entry);
  },
  async clear() {
    await logDb.logs.clear();
  },
  async readPage(opts) {
    if (opts.beforeId !== undefined) {
      return await logDb.logs
        .orderBy('ts')
        .reverse()
        .filter(e => (e.id ?? 0) < (opts.beforeId as number))
        .limit(opts.limit)
        .toArray();
    }
    return await logDb.logs.orderBy('ts').reverse().limit(opts.limit).toArray();
  },
  async listTypes() {
    const all = await logDb.logs.orderBy('ts').toArray();
    // First aggregate by top-level type bucket, then by day within each type (for the dayCount count).
    const byType = new Map<ClientLogType, { count: number; days: Set<number> }>();
    for (const e of all) {
      const type = typeOfEntry(e);
      const cur = byType.get(type) ?? { count: 0, days: new Set<number>() };
      cur.count += 1;
      cur.days.add(startOfLocalDay(e.ts));
      byType.set(type, cur);
    }
    // Always return all three top-level buckets in UI order (count=0 when empty), mirroring the server's fixed three directories.
    return CLIENT_LOG_TYPES.map(type => {
      const v = byType.get(type);
      return {
        type,
        count: v?.count ?? 0,
        dayCount: v?.days.size ?? 0
      } satisfies ClientLogTypeInfo;
    });
  },
  async listBucketsForType(type) {
    const scoped = logDb.logs.where('[type+ts]').between([type, Dexie.minKey], [type, Dexie.maxKey]).toArray();
    const rows = await scoped;
    // Only count entries whose persisted type matches this bucket; old data missing `type` is inferred via typeOfEntry and filed into the matching bucket.
    const effective: ClientLogEntry[] = [];
    for (const e of rows) {
      const t = typeOfEntry(e);
      if (t === type) effective.push(e);
    }
    // Aggregate counts by "day", keeping the earliest ts of each day for computing the sub-bucket interval.
    const byDay = new Map<number, { count: number; minTs: number }>();
    for (const e of effective) {
      const start = startOfLocalDay(e.ts);
      const cur = byDay.get(start);
      if (cur) {
        cur.count += 1;
        if (e.ts < cur.minTs) cur.minTs = e.ts;
      } else {
        byDay.set(start, { count: 1, minTs: e.ts });
      }
    }
    const nowStart = startOfLocalDay(Date.now());
    return (
      [...byDay.entries()]
        .map(([start, v]) => {
          const is_current = start === nowStart;
          // The sub-bucket's right endpoint is "that day's midnight + 1 day". To avoid boundary rounding
          // artifacts at timezone / day-crossing edges,
          // each bucket uses the local midnight obtained by truncating its earliest record's ts (identical to start).
          return {
            type,
            name: bucketNameOf(v.minTs),
            tsStart: start,
            tsEnd: start + DAY_MS,
            count: v.count,
            is_current
          } satisfies ClientLogBucket;
        })
        // Newest (today) first, mirroring the server file list's "newest first".
        .sort((a, b) => b.tsStart - a.tsStart)
    );
  },
  async readBucket(t) {
    const filtered = (rows: ClientLogEntry[]): ClientLogEntry[] =>
      t.type === 'all' ? rows : rows.filter(e => typeOfEntry(e) === t.type);
    return await logDb.logs
      .where('ts')
      .aboveOrEqual(t.tsStart)
      .and(e => e.ts < t.tsEnd)
      .sortBy('ts') // ascending; reversed afterwards so results come back newest first
      .then(rows =>
        filtered(rows)
          .reverse()
          .slice(0, t.limit ?? 500)
      );
  }
};

/**
 * The currently active persistence layer. Defaults to Dexie; tests can inject an in-memory fake via {@link setClientLogStore}.
 * Kept as a mutable variable (rather than a constant) so tests can override it.
 */
let activeStore: ClientLogStore = dexieStore;

/** In-memory render buffer (for instant display while the dialog is open; cross-restart history lives in Dexie). */
const logBuffer: ClientLogEntry[] = [];

/** Set of live subscribers: while the dialog is open, newly captured entries are appended to the view in real time. */
const clientLogSubscribers = new Set<(entry: ClientLogEntry) => void>();

let captureInstalled = false;

/**
 * Override the persistence layer (for tests). Pass `null` to restore the default Dexie implementation.
 */
export function setClientLogStore(store: ClientLogStore | null): void {
  activeStore = store ?? dexieStore;
}

/** References to the original console methods; after capture, calls are forwarded to these real implementations. */
const origConsole = {
  debug: console.debug,
  log: console.log,
  info: console.info,
  warn: console.warn,
  error: console.error
};

/**
 * Write one captured entry into both the (length-capped) in-memory buffer and Dexie (history).
 * The Dexie write is fire-and-forget: it never blocks the console call itself.
 */
function pushEntry(level: string, text: string): void {
  if (!text) return;
  const entry: ClientLogEntry = { level, type: levelToType(level), text, ts: Date.now() };

  logBuffer.push(entry);
  if (logBuffer.length > MAX_LOG_RECS) {
    logBuffer.splice(0, logBuffer.length - MAX_LOG_RECS);
  }

  // Persist to history (async; failures must not block the console).
  activeStore
    .add(entry)
    .then(() => {})
    .catch(e => {
      // When IndexedDB is unavailable (private browsing mode / broken), silently degrade to in-memory-only capture.
      console.warn('[clientLog] failed to persist entry:', e);
    });

  // Notify open views in real time.
  clientLogSubscribers.forEach(fn => fn(entry));
}

/** Format a list of console args into a single line of text. */
function formatArgs(args: unknown[]): string {
  let text = '';
  for (const a of args) {
    let s: string;
    try {
      s = typeof a === 'string' ? a : JSON.stringify(a, null, 0);
    } catch {
      s = String(a);
    }
    if (s === undefined || s === '') continue;
    text = text ? `${text} ${s}` : s;
  }
  return text;
}

/**
 * Install the browser `console.*` capture. Installed at most once per process.
 *
 * Captured output goes into both the in-memory buffer and the Dexie history; the installation
 * itself is non-invasive to console calls — it only intercepts and forwards them.
 */
export function installClientLogCapture(): void {
  if (captureInstalled) return;
  captureInstalled = true;

  console.debug = (...args: unknown[]) => {
    const text = formatArgs(args);
    if (text) pushEntry('DEBUG', `${new Date().toLocaleTimeString()} | ${'DEBUG'.padEnd(8, ' ')} | ${text}`);
    origConsole.debug.apply(console, args as never[]);
  };
  console.log = (...args: unknown[]) => {
    const text = formatArgs(args);
    if (text) pushEntry('INFO', `${new Date().toLocaleTimeString()} | ${'INFO'.padEnd(8, ' ')} | ${text}`);
    origConsole.log.apply(console, args as never[]);
  };
  console.info = (...args: unknown[]) => {
    const text = formatArgs(args);
    if (text) pushEntry('INFO', `${new Date().toLocaleTimeString()} | ${'INFO'.padEnd(8, ' ')} | ${text}`);
    origConsole.info.apply(console, args as never[]);
  };
  console.warn = (...args: unknown[]) => {
    const text = formatArgs(args);
    if (text) pushEntry('WARNING', `${new Date().toLocaleTimeString()} | ${'WARNING'.padEnd(8, ' ')} | ${text}`);
    origConsole.warn.apply(console, args as never[]);
  };
  console.error = (...args: unknown[]) => {
    const text = formatArgs(args);
    if (text) pushEntry('ERROR', `${new Date().toLocaleTimeString()} | ${'ERROR'.padEnd(8, ' ')} | ${text}`);
    origConsole.error.apply(console, args as never[]);
  };
}

/**
 * Read client log history (reverse order: newest first).
 *
 * Supports "history logs": when the dialog opens, read an initial batch of recent entries,
 * then catch up via the live subscription.
 *
 * @param options.limit   Maximum number of entries to return (default 500).
 * @param options.beforeId Optional: only return entries with `id < beforeId`, for paging upward into older history.
 * @returns Array of entries in reverse time order (**newest first**).
 */
export async function readClientLogs(options: { limit?: number; beforeId?: number } = {}): Promise<ClientLogEntry[]> {
  const limit = options.limit ?? 500;
  return await activeStore.readPage({
    limit,
    ...(options.beforeId !== undefined ? { beforeId: options.beforeId } : {})
  });
}

/** Clear client logs: persisted history + in-memory buffer. */
export async function clearClientLogs(): Promise<void> {
  logBuffer.length = 0;
  await activeStore.clear();
}

/**
 * List the client log top-level type buckets (`all`/`log`/`error`), for the "bucket by type" dropdown — mirrors the server's three directories.
 */
export async function listClientLogTypes(): Promise<ClientLogTypeInfo[]> {
  return await activeStore.listTypes();
}

/**
 * List the per-day sub-buckets inside a type bucket (newest first), for the "bucket by day" dropdown — mirrors the server's per-day file list.
 */
export async function listClientLogBucketsForType(type: ClientLogType): Promise<ClientLogBucket[]> {
  return await activeStore.listBucketsForType(type);
}

/**
 * Read the logs inside one type's per-day bucket (newest first). `bucket.tsStart/tsEnd` come from {@link listClientLogBucketsForType}.
 *
 * @param bucket Target bucket metadata (including its type).
 * @param limit  Maximum number of entries to return (default 500).
 */
export async function readClientLogBucket(bucket: ClientLogBucket, limit?: number): Promise<ClientLogEntry[]> {
  return await activeStore.readBucket({ type: bucket.type, tsStart: bucket.tsStart, tsEnd: bucket.tsEnd, limit });
}

/** Return a snapshot of the current in-memory buffer (newest first). */
export function getLogBufferSnapshot(): ClientLogEntry[] {
  return [...logBuffer];
}

/** Subscribe to live client log pushes. Returns an unsubscribe function. */
export function subscribeClientLogs(fn: (entry: ClientLogEntry) => void): () => void {
  clientLogSubscribers.add(fn);
  return () => {
    clientLogSubscribers.delete(fn);
  };
}
