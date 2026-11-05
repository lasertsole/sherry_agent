/**
 * Per-entry long-term memory CRUD (`/memory/entries*`).
 *
 * The legacy full-file `GET/PUT /memory` pair stays in `bridge/memory.ts`;
 * this module is the memory panel's CRUD surface: paged listing, create,
 * edit, archive/restore, stats, search and the usage-aging trigger.
 *
 * @module bridge/memory-entries
 */

/** The three long-term memory targets. */
export type MemoryTarget = 'memory' | 'user' | 'facts';

/** Archive reasons the backend accepts. */
export type MemoryArchiveReason = 'manual' | 'evicted' | 'contradicted' | 'consolidated' | 'superseded';

/** One memory row as the API returns it. */
export interface MemoryEntry {
  id: number;
  target: MemoryTarget;
  content: string;
  entry_type: string | null;
  origin_session: string | null;
  created_at: number;
  updated_at: number | null;
  last_used: number | null;
  usage_count: number;
  char_count: number;
  archived: boolean;
  archived_at: number | null;
  archived_reason: string | null;
}

/** `GET /memory/entries` payload. */
export interface MemoryEntryPage {
  entries: MemoryEntry[];
  total: number;
  limit: number;
  offset: number;
  target: MemoryTarget | null;
}

/** One target's aggregate row in `GET /memory/entries/stats`. */
export interface MemoryTargetStats {
  count: number;
  chars: number;
  avg_usage: number;
  archived: number;
  limit: number;
  percent: number;
  file: string;
}

/** UI-editable limits returned by `GET /memory/settings`. */
export interface MemorySettings {
  memory_char_limit: number;
  user_char_limit: number;
  facts_char_limit: number;
  max_unused_days: number;
  semantic_recall_enabled: boolean;
  semantic_recall_top_k: number;
  semantic_recall_char_budget: number;
}

/** `GET /memory/entries/stats` payload. */
export interface MemoryStats {
  targets: Record<MemoryTarget, MemoryTargetStats>;
  settings: MemorySettings;
  total_entries: number;
  total_chars: number;
}

/** `POST /memory/search` payload. */
export interface MemorySearchResult {
  entries: MemoryEntry[];
  total: number;
  query: string;
  mode?: 'semantic' | 'substring';
}

/** `POST /memory/consolidate` acknowledgment. */
export interface MemoryConsolidationTask {
  task_id: string;
  status: 'running' | 'completed' | 'skipped' | 'failed';
  result?: {
    status?: string;
    reason?: string;
    archived?: number;
    entries_before?: number;
    entries_after?: number;
    reply?: string;
  };
  error?: string | null;
}

/** Parameters for {@link readMemoryEntries}. */
export interface ReadMemoryEntriesParams {
  target?: MemoryTarget | null;
  includeArchived?: boolean;
  sort?: 'created' | 'updated' | 'usage' | 'last_used';
  order?: 'asc' | 'desc';
  limit?: number;
  offset?: number;
}

function queryString(params: Record<string, string | number | boolean | null | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue;
    search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : '';
}

/**
 * Page through the active (or archived) entries of one target.
 * @param params
 */
export async function readMemoryEntries(params: ReadMemoryEntriesParams = {}): Promise<MemoryEntryPage> {
  return fetchApiPayload<MemoryEntryPage>({
    url: `/memory/entries${queryString({
      target: params.target,
      include_archived: params.includeArchived ? 'true' : undefined,
      sort: params.sort,
      order: params.order,
      limit: params.limit,
      offset: params.offset
    })}`,
    method: 'get'
  });
}

/**
 * Create one entry.
 * @param body
 * @param body.target
 * @param body.content
 * @param body.entry_type
 */
export async function createMemoryEntry(body: {
  target: MemoryTarget;
  content: string;
  entry_type?: string | null;
}): Promise<MemoryEntry> {
  return fetchApiPayload<MemoryEntry>({ url: '/memory/entries', method: 'post', opts: body });
}

/**
 * Rewrite one entry's body (or its type tag).
 * @param id
 * @param body
 * @param body.content
 * @param body.entry_type
 */
export async function updateMemoryEntry(
  id: number,
  body: { content?: string; entry_type?: string | null }
): Promise<MemoryEntry> {
  return fetchApiPayload<MemoryEntry>({ url: `/memory/entries/${id}`, method: 'put', opts: body });
}

/**
 * Soft-delete one entry.
 * @param id
 * @param reason
 */
export async function archiveMemoryEntry(id: number, reason: MemoryArchiveReason = 'manual'): Promise<MemoryEntry> {
  return fetchApiPayload<MemoryEntry>({
    url: `/memory/entries/${id}`,
    method: 'delete',
    opts: { reason }
  });
}

/**
 * Restore an archived entry.
 * @param id
 */
export async function restoreMemoryEntry(id: number): Promise<MemoryEntry> {
  return fetchApiPayload<MemoryEntry>({ url: `/memory/entries/${id}/restore`, method: 'post' });
}

/** Per-target counts, character usage and limits. */
export async function readMemoryStats(): Promise<MemoryStats> {
  return fetchApiPayload<MemoryStats>({ url: '/memory/entries/stats', method: 'get' });
}

/**
 * Substring search over the active entries (no usage increment).
 * @param query
 * @param target
 */
export async function searchMemoryEntries(query: string, target?: MemoryTarget | null): Promise<MemorySearchResult> {
  return fetchApiPayload<MemorySearchResult>({
    url: '/memory/search',
    method: 'post',
    opts: { query, target: target ?? null }
  });
}

/** Read the UI-editable memory limits. */
export async function readMemorySettings(): Promise<MemorySettings> {
  return fetchApiPayload<MemorySettings>({ url: '/memory/settings', method: 'get' });
}

/**
 * Persist the UI-editable memory limits.
 * @param body
 */
export async function writeMemorySettings(
  body: Partial<
    Pick<
      MemorySettings,
      | 'memory_char_limit'
      | 'user_char_limit'
      | 'facts_char_limit'
      | 'max_unused_days'
      | 'semantic_recall_enabled'
      | 'semantic_recall_top_k'
      | 'semantic_recall_char_budget'
    >
  >
): Promise<MemorySettings> {
  return fetchApiPayload<MemorySettings>({ url: '/memory/settings', method: 'put', opts: body });
}

/**
 * Start the async aging + LLM consolidation pass for one session.
 * @param sessionId
 */
export async function consolidateMemory(sessionId: string): Promise<MemoryConsolidationTask> {
  return fetchApiPayload<MemoryConsolidationTask>({
    url: '/memory/consolidate',
    method: 'post',
    opts: { session_id: sessionId }
  });
}

/**
 * Poll one consolidation task.
 * @param taskId
 */
export async function readConsolidationStatus(taskId: string): Promise<MemoryConsolidationTask> {
  return fetchApiPayload<MemoryConsolidationTask>({
    url: `/memory/consolidate/${encodeURIComponent(taskId)}`,
    method: 'get'
  });
}
