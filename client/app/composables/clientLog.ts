import Dexie, { type Table } from 'dexie';

/**
 * 客户端日志的类型大桶（对等 server logs/logger.py 的 `info`/`all`/`error` 三目录）。
 * 顺序即 UI 展示顺序：`all`（全量）、`log`（INFO 流水）、`error`（错误）。
 */
export const CLIENT_LOG_TYPES = ['all', 'log', 'error'] as const;
export type ClientLogType = (typeof CLIENT_LOG_TYPES)[number];

/**
 * 持久化存储抽象：默认实现指向 Dexie（IndexedDB），便于测试注入内存假实现。
 *
 * 分桶语义：**先按类型分大桶（type），再在类型内按天分小桶（date）**，
 * 与 server 的「目录（info/all/error）/ 按天文件」结构对等。
 */
export interface ClientLogStore {
  add(entry: ClientLogEntry): Promise<number>;
  clear(): Promise<void>;
  /** 读取历史（最新在前）。`beforeId` 存在时仅返回 `id < beforeId` 的记录。 */
  readPage(opts: { limit: number; beforeId?: number }): Promise<ClientLogEntry[]>;
  /** 列出类型大桶元数据（含总条数、天数），用于「按类型分大桶」下拉——对等 server 的三个日志目录。 */
  listTypes(): Promise<ClientLogTypeInfo[]>;
  /** 列出某个类型大桶内部的日期小桶（最新在前），用于「按天分小桶」下拉。 */
  listBucketsForType(type: ClientLogType): Promise<ClientLogBucket[]>;
  /** 读取某个类型下某天分桶内的日志（最新在前）。`tsStart` 为当天 0 点，`tsEnd` 为次 0 点（半开区间）。 */
  readBucket(t: { type: ClientLogType; tsStart: number; tsEnd: number; limit?: number }): Promise<ClientLogEntry[]>;
}

/**
 * 前端（客户端）日志捕获与持久化。
 *
 * 目标：让「日志查看 → 客户端」Tab 具备与「服务端」Tab 对等的 **历史 + 实时** 能力。
 *
 * - **实时**：安装一次浏览器 `console.*` 捕获，改动后即向 {@link clientLogSubscribers} 推送新增记录；
 * - **历史**：每次捕获同步写入 IndexedDB（Dexie）`client_logs` 表，应用重启后通过
 *   {@link readClientLogs} 重建记录，真正做到跨会话留存（与 only-memory 的旧行为不同）；
 * - **清理**：{@link clearClientLogs} 清空 Dexie 与内存缓冲，避免历史无限膨胀。
 *
 * 捕获仅作用于前端 Vue/浏览器侧的 `console.*` 调用；Rust tracing（`src-tauri`）日志不在此列。
 */

/** 单条客户端日志记录。 */
export interface ClientLogEntry {
  /** Dexie 自增主键，用于历史分页（`beforeId`）与排序。 */
  id?: number;
  /** 日志级别（大写，与 server tab 着色规则一致）。 */
  level: string;
  /** 已格式化的文本（包含时间戳头 `HH:MM:SS | LEVEL | message`）。 */
  text: string;
  /** 捕获时刻的 Unix 毫秒时间戳（用于按时间排序）。 */
  ts: number;
  /**
   * 所属类型大桶（`all`/`log`/`error`），由 {@link levelToType} 在捕获时推断并持久化，
   * 便于按类型高效检索。历史数据（无此字段）读取时用 {@link typeOfEntry} 兜底推断。
   */
  type?: ClientLogType;
}

/**
 * 把日志级别映射到类型大桶——镜像 server logs/logger.py 的三目录语义：
 * - `all`：所有级别；`log`（对等 info 目录）仅 INFO；`error`：ERROR + CRITICAL。
 * 客户端 console 捕获实际只会产生 DEBUG/INFO/WARNING/ERROR，但补全 TRACE/SUCCESS
 * 与未知级别，使分类与 server 完全对齐。
 */
export function levelToType(level: string): ClientLogType {
  switch ((level || '').toUpperCase()) {
    case 'ERROR':
    case 'CRITICAL':
      return 'error';
    case 'INFO':
      return 'log';
    default:
      // TRACE / DEBUG / SUCCESS / WARNING / 未知级别 均归入 all（与 server 的 all 桶一致）。
      return 'all';
  }
}

/** 取一条记录的类型：优先用持久化字段，缺失时从级别推断（兼容旧历史数据）。 */
export function typeOfEntry(entry: ClientLogEntry): ClientLogType {
  return entry.type ?? levelToType(entry.level);
}

/** 内存渲染缓冲上限：超出后丢弃最旧的记录。 */
export const MAX_LOG_RECS = 5000;

/**
 * 类型大桶元数据——对等 server 端 logs/output 下的 `info`/`all`/`error` 三个目录。
 * 用于「按类型分大桶」下拉的第一级。
 */
export interface ClientLogTypeInfo {
  /** 类型标识（`all`/`log`/`error`）。 */
  type: ClientLogType;
  /** 该类型下的总日志条数。 */
  count: number;
  /** 该类型包含的日期小桶（天数）。 */
  dayCount: number;
}

/**
 * 某个类型大桶内的日期小桶元数据——对等 server 端 `LogFileInfo`（按天文件）。
 *
 * - 每条客户端日志按捕获时刻归入其「当天」分桶，同时归入由级别推断出的类型大桶；
 * - 日期小桶即下拉里的一个「文件」；「今天」的日期小桶视为当前（is_current=true），
 *   只有它（且对应类型）可以开启实时流（对等 server 只有当前进程日志可实时推送）。
 */
export interface ClientLogBucket {
  /** 所属类型大桶。 */
  type: ClientLogType;
  /** 分桶展示名，如 `2026-08-17`。 */
  name: string;
  /** 该天 0 点（本地时区）Unix 毫秒时间戳，作为读取区间的左端点。 */
  tsStart: number;
  /** 次天 0 点 Unix 毫秒时间戳（半开区间右端点）。 */
  tsEnd: number;
  /** 该分桶内的日志条数。 */
  count: number;
  /** 是否为「今天」的分桶（唯一可开启实时流）。 */
  is_current: boolean;
}

/** 本地日期分桶粒度：每天一个桶。 */
const DAY_MS = 24 * 60 * 60 * 1000;

/** 将 Unix 毫秒时间戳归零到本地时区当天 0 点。 */
export function startOfLocalDay(ts: number): number {
  const d = new Date(ts);
  d.setHours(0, 0, 0, 0);
  return d.getTime();
}

/** 计算某时间戳所属分桶的展示名（本地时区，形如 `YYYY-MM-DD`）。 */
export function bucketNameOf(ts: number): string {
  const d = new Date(ts);
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

class ClientLogDb extends Dexie {
  /** 客户端日志表：主键自增 `id`，每条含 `ts` 与 `type` 便于历史分页与类型检索。 */
  logs!: Table<ClientLogEntry, number>;

  constructor() {
    super('ema-client-log-cache');
    this.version(2).stores({
      // `ts` 建立索引以便按时间倒序读取历史；`type` + `type&ts` 支持按类型大桶聚合与区间查询；
      // 主键 `id` 自增天然有序。旧历史无 `type` 字段，读取时用 typeOfEntry 兜底推断。
      logs: '++id, ts, type, [type+ts]',
    });
  }
}

/** 全局唯一的客户端日志数据库实例。 */
export const logDb = new ClientLogDb();

/** 默认（生产）持久化层：把读写转发到 Dexie。 */
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
        .filter((e) => (e.id ?? 0) < (opts.beforeId as number))
        .limit(opts.limit)
        .toArray();
    }
    return await logDb.logs.orderBy('ts').reverse().limit(opts.limit).toArray();
  },
  async listTypes() {
    const all = await logDb.logs.orderBy('ts').toArray();
    // 先按类型大桶聚合，再在每个类型内按天聚合（供 dayCount 计数）。
    const byType = new Map<ClientLogType, { count: number; days: Set<number> }>();
    for (const e of all) {
      const type = typeOfEntry(e);
      const cur = byType.get(type) ?? { count: 0, days: new Set<number>() };
      cur.count += 1;
      cur.days.add(startOfLocalDay(e.ts));
      byType.set(type, cur);
    }
    // 固定按 UI 顺序返回全部三个大桶（无数据的 count=0），对等 server 固定三目录。
    return CLIENT_LOG_TYPES.map((type) => {
      const v = byType.get(type);
      return {
        type,
        count: v?.count ?? 0,
        dayCount: v?.days.size ?? 0,
      } satisfies ClientLogTypeInfo;
    });
  },
  async listBucketsForType(type) {
    const scoped = logDb.logs.where('[type+ts]').between([type, Dexie.minKey], [type, Dexie.maxKey]).toArray();
    const rows = await scoped;
    // 只统计持久化 type 匹配本桶的记录；缺失 type 的旧数据由 typeOfEntry 推断后归入对应桶。
    const effective: ClientLogEntry[] = [];
    for (const e of rows) {
      const t = typeOfEntry(e);
      if (t === type) effective.push(e);
    }
    // 按「天」聚合计数，并保留每天最早一条的 ts 用于计算小桶区间。
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
    return [...byDay.entries()]
      .map(([start, v]) => {
        const is_current = start === nowStart;
        // 小桶右端点为「该天 0 点 + 1 天」。为避免时区/跨日边界的边界舍入偏差，
        // 对每个桶使用从记录最早 ts 归零得到的当天 0 点（与 start 一致）。
        return {
          type,
          name: bucketNameOf(v.minTs),
          tsStart: start,
          tsEnd: start + DAY_MS,
          count: v.count,
          is_current,
        } satisfies ClientLogBucket;
      })
      // 最新（今天）在前，对等 server 文件列表「newest first」。
      .sort((a, b) => b.tsStart - a.tsStart);
  },
  async readBucket(t) {
    const filtered = (rows: ClientLogEntry[]): ClientLogEntry[] =>
      t.type === 'all' ? rows : rows.filter((e) => typeOfEntry(e) === t.type);
    return await logDb.logs
      .where('ts')
      .aboveOrEqual(t.tsStart)
      .and((e) => e.ts < t.tsEnd)
      .sortBy('ts') // 升序，随后倒序返回（最新在前）
      .then((rows) => filtered(rows).reverse().slice(0, t.limit ?? 500));
  },
};

/**
 * 当前生效的持久化层。默认 Dexie；测试可通过 {@link setClientLogStore} 注入内存假实现。
 * 变更为可写变量以便测试覆盖，而非常量。
 */
let activeStore: ClientLogStore = dexieStore;

/** 内存渲染缓冲（用于弹窗打开时的即时展示；跨重启历史见 Dexie）。 */
const logBuffer: ClientLogEntry[] = [];

/** 实时订阅器集合：弹窗打开期间收到新增记录时实时追加到视图。 */
const clientLogSubscribers = new Set<(entry: ClientLogEntry) => void>();

let captureInstalled = false;

/**
 * 覆盖持久化层（测试用）。传入 `null` 恢复默认 Dexie 实现。
 */
export function setClientLogStore(store: ClientLogStore | null): void {
  activeStore = store ?? dexieStore;
}

/** 原文 console 方法引用，捕获后透传给真实实现。 */
const origConsole = {
  debug: console.debug,
  log: console.log,
  info: console.info,
  warn: console.warn,
  error: console.error,
};

/**
 * 把一条捕获记录同时写入内存缓冲（限长）与 Dexie（历史）。
 * 写入 Dexie 采用 fire-and-forget：不阻塞 console 调用本身。
 */
function pushEntry(level: string, text: string): void {
  if (!text) return;
  const entry: ClientLogEntry = { level, type: levelToType(level), text, ts: Date.now() };

  logBuffer.push(entry);
  if (logBuffer.length > MAX_LOG_RECS) {
    logBuffer.splice(0, logBuffer.length - MAX_LOG_RECS);
  }

  // 历史落盘（异步，失败不阻断控制台）。
  activeStore
    .add(entry)
    .then(() => {})
    .catch((e) => {
      // IndexedDB 不可用时（私隐模式 / 失效），静默降级为纯内存捕获。
      console.warn('[clientLog] failed to persist entry:', e);
    });

  // 实时通知打开的视图。
  clientLogSubscribers.forEach((fn) => fn(entry));
}

/** 把 console arg 列表格式化为单行文本。 */
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
 * 安装浏览器 `console.*` 捕获。进程内仅安装一次。
 *
 * 捕获结果同时进入内存缓冲 + Dexie 历史；安装动作本身对控制台调用无侵入，
 * 只是拦截后透传。
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
 * 读取客户端日志历史（倒序：最新在前）。
 *
 * 用于支持「历史日志」：弹窗打开时先读一段最近记录，之后靠实时订阅追平。
 *
 * @param options.limit   返回条数上限（默认 500）。
 * @param options.beforeId 可选：只返回 `id < beforeId` 的记录，用于向上翻页加载更早历史。
 * @returns 按时间**倒序**（最新在前）返回的记录数组。
 */
export async function readClientLogs(options: { limit?: number; beforeId?: number } = {}): Promise<ClientLogEntry[]> {
  const limit = options.limit ?? 500;
  return await activeStore.readPage({
    limit,
    ...(options.beforeId !== undefined ? { beforeId: options.beforeId } : {}),
  });
}

/** 清空客户端日志：持久化历史 + 内存缓冲。 */
export async function clearClientLogs(): Promise<void> {
  logBuffer.length = 0;
  await activeStore.clear();
}

/**
 * 列出客户端日志的类型大桶（`all`/`log`/`error`），用于「按类型分大桶」下拉——对等 server 的三目录。
 */
export async function listClientLogTypes(): Promise<ClientLogTypeInfo[]> {
  return await activeStore.listTypes();
}

/**
 * 列出某个类型大桶内的日期小桶（最新在前），用于「按天分小桶」下拉——对等 server 按天的文件列表。
 */
export async function listClientLogBucketsForType(type: ClientLogType): Promise<ClientLogBucket[]> {
  return await activeStore.listBucketsForType(type);
}

/**
 * 读取某个类型下某天分桶内的日志（最新在前）。`bucket.tsStart/tsEnd` 取自 {@link listClientLogBucketsForType}。
 *
 * @param bucket 目标分桶元数据（含所属类型）。
 * @param limit  返回条数上限（默认 500）。
 */
export async function readClientLogBucket(
  bucket: ClientLogBucket,
  limit?: number,
): Promise<ClientLogEntry[]> {
  return await activeStore.readBucket({ type: bucket.type, tsStart: bucket.tsStart, tsEnd: bucket.tsEnd, limit });
}

/** 返回当前内存缓冲的一份快照（最新在前）。 */
export function getLogBufferSnapshot(): ClientLogEntry[] {
  return [...logBuffer];
}

/** 订阅客户端日志实时推送。返回取消订阅函数。 */
export function subscribeClientLogs(fn: (entry: ClientLogEntry) => void): () => void {
  clientLogSubscribers.add(fn);
  return () => {
    clientLogSubscribers.delete(fn);
  };
}
