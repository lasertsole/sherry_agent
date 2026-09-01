import Dexie, { type IndexableType, type Table } from 'dexie';
import { DEFAULT_CHARACTER } from './defaultCharacter';
import type { MessageItem } from '@/pages/home/type';

/** Lower bound of the compound index (smallest encoded value sharing the same session_id prefix) */
const MIN_KEY = Dexie.minKey as IndexableType;

/** Upper bound of the compound index (largest encoded value sharing the same session_id prefix) */
const MAX_KEY = Dexie.maxKey as IndexableType;

/**
 * Cached conversation history message records.
 *
 * Fields stay consistent with the backend's `messages` table in
 * `context_engine/store/db.py`, so rows returned by `/get_history_by_turn_page`
 * can be cached as-is (deduplication key: `id`).
 */
export interface CachedMessage {
  /** Auto-increment primary key from the database (deduplication basis) */
  id: number;
  /** Turn number */
  turn_num: number;
  /** Session ID */
  session_id: string;
  role: string;
  content: string | null;
  timestamp: string | null;
  /** Image array (matches the backend message rows; the history API has already parsed JSON into arrays).
   *  User messages store base64 (no data: prefix), AI messages store absolute file paths after persistence. */
  images: string[] | null;
  audios: string[] | null;
  videos: string[] | null;
  tool_call_id: string | null;
  tool_calls: string | null;
  tool_status: string | null;
  tool_name: string | null;
  finish_reason: string | null;
  reasoning: string | null;
  reasoning_content: string | null;
  /** Model name (from the backend history row's model_name) */
  model_name: string | null;
  /** Input token count (from the backend history row's input_tokens) */
  input_tokens: number | null;
  /** Output token count (from the backend history row's output_tokens) */
  output_tokens: number | null;
}

/**
 * Cached character display info (avatar + name), keyed by `session_id`.
 *
 * Each session snapshots the "global pending profile" once when first opened and locks it to
 * that session's row. Therefore updating the avatar/name in system config afterwards only
 * affects new sessions (which snapshot the latest global profile again); old sessions keep
 * the snapshot taken when they were opened.
 *
 * An avatar can be a base64 data URL (`data:image/...;base64,...`, user-uploaded) or a
 * `/avatar/xxx.jpg` relative URL (built-in default, from `defaultCharacter.ts`);
 * the frontend `<img>` renders both directly, with no need to prepend the `static/` path.
 */
export interface CachedCharacter {
  /** Session ID; the global pending profile uses {@link GLOBAL_SESSION_KEY} as its primary key */
  session_id: string;
  userName: string;
  /** base64 data URL or `/avatar/xxx.jpg` relative URL */
  userAvatar: string;
  aiName: string;
  /** base64 data URL or `/avatar/xxx.jpg` relative URL */
  aiAvatar: string;
}

/**
 * Cached session-list entry (locally persisted placeholder for an empty session).
 *
 * The backend session list is derived from the message table, so when an empty session is
 * created (no messages sent yet) no corresponding record exists server-side. To cover the
 * offline scenario "a newly created conversation survives a refresh", the frontend persists
 * these placeholder entries in IndexedDB. They mirror the in-memory `historyList` state and
 * can be restored after refresh / restart.
 */
export interface CachedSessionMeta {
  /** Session ID */
  id: string;
  /** Session title (placeholder title for newly created unnamed sessions) */
  title: string;
  /** Creation time (local time string, shown in the left-hand list) */
  createTime: string;
  /** Local sort timestamp (used for newest-first merge sorting) */
  updatedAt: number;
}

/**
 * Session custom-title override layer: separate from the sessions placeholder table so it
 * survives session promotion.
 *
 * The server-side session title is derived at read time from "the last user message"
 * (no server-side title storage). After the user renames a session inline in the left-hand
 * list, the custom title is written here and overrides the derived title when loading the list.
 */
export interface SessionTitleOverride {
  /** Session ID */
  id: string;
  /** User-defined custom title */
  title: string;
  /** Timestamp of the last rename (Date.now()) */
  updatedAt: number;
}

/**
 * User-uploaded chat-area background image (shown in both light/dark themes).
 *
 * A single global row (primary key {@link GLOBAL_SESSION_KEY}) stores one base64 data URL
 * plus the themed overlay opacity; it is not per-session. Read/written from the system
 * config "background settings".
 */
export interface CachedBackground {
  /** Global unique primary key, fixed to {@link GLOBAL_SESSION_KEY} */
  session_id: string;
  /** Background image base64 data URL (`data:image/...;base64,...`); empty string means not set yet */
  backgroundUrl: string;
  /** Overlay opacity (integer 0-100). White overlay in the light theme, black overlay in the dark theme;
   *  the higher the value, the closer the photo gets to pure white/pure black until fully washed out. Default 0 means no overlay. */
  backgroundOpacity: number;
}

/**
 * Draft cache record for a whole in-flight agent turn.
 *
 * The backend `aafter_agent` only triggers persistence once per turn, and only when the
 * entire LangGraph node returns successfully. Tool-calling phases, as well as turns that
 * error out or get aborted midway, are **never persisted server-side**. To avoid "losing
 * the stage content already produced earlier when the agent replies 'greeting → analysis →
 * tool call → tool result observation → final result' because some step errors out or the
 * final result is never emitted", the frontend caches the entire in-flight `MessageItem[]`
 * into the local IndexedDB `drafts` table.
 *
 * The key is `[session_id + turn_num]`: `turn_num` uses the same **positive turn number**
 * as live messages (see {@link DraftTurn.turn_num}). The server's `aafter_agent` only
 * persists a turn after the whole LangGraph node returns successfully; in-flight / error /
 * aborted turns produce no server row at all, so a draft's `turn_num` never collides with
 * any persisted row. Drafts live in the separate `drafts` table and do not pollute
 * `cachedMaxTurnNum` (that function only queries the `messages` table).
 *
 * Each step (send / tool_start / tool_end / tool_result / error / first text chunk /
 * server onDone) rewrites this row wholesale, achieving "cache per step"; text appends are
 * merged with a ~200ms debounce. The row is cleared after the server persists successfully
 * (reconciled via onDone / `loadSessionHistory`).
 */
export interface DraftTurn {
  /** Session ID */
  session_id: string;
  /**
   * This turn's number, consistent with the incrementing positive `turn_num` within the same
   * session in the server's `messages` table. Each `MessageItem` inside the draft row's
   * `messages` uses a local negative temporary id (`tempIdCounter` incrementing from
   * -1000000, so ascending id order within a turn equals creation order); during
   * reconciliation, draft rows with negative ids can be replaced by the server's positive-id
   * rows to complete deduplication.
   */
  turn_num: number;
  /** All messages of the in-flight turn (local negative temporary ids; ascending id order equals creation order) */
  messages: MessageItem[];
}

/**
 * Cached subagent run records.
 *
 * Pushed in real time by the backend `/subagents/ws` WebSocket (events `subagent_spawned` /
 * `subagent_ended`), and backfilled (gap filling) via `GET /subagents/runs` after first
 * load / session switch / WS reconnection. Fields stay consistent with the backend's
 * `_PUBLIC_FIELDS` in `server/trigger/http/subagent.py`.
 *
 * Primary key is `run_id`; `requester_session_key` is used to highlight, in the frontend
 * "background tasks" view, the subagents spawned by the current session.
 */
export interface CachedSubagentRun {
  run_id: string;
  child_session_key: string | null;
  requester_session_key: string | null;
  task: string | null;
  task_name: string | null;
  label: string | null;
  spawn_mode: string | null;
  context_mode: string | null;
  agent_id: string | null;
  depth: number | null;
  role: string | null;
  control_scope: string | null;
  generation: number | null;
  swarm_group_id: string | null;
  swarm_run_state: string | null;
  ended_reason: string | null;
  pause_reason: string | null;
  execution: {
    status?: string | null;
    outcome?: string | null;
    started_at?: string | null;
    completed_at?: string | null;
  } | null;
  completion: {
    required?: boolean | null;
    owner_session_key?: string | null;
    result_text?: string | null;
    captured_at?: string | null;
  } | null;
  delivery: { status?: string | null; attempt_count?: number | null; delivered_at?: string | null } | null;
}

/**
 * Locally persisted AI persona preset: a named snapshot of the four workspace persona
 * files, saved/restored from the "persona dialog".
 *
 * `content` maps each persona file basename to its full text; keys are exactly
 * 'AGENTS.md' / 'IDENTITY.md' / 'SOUL.md' / 'USER.md' (the same basenames used by the
 * persona dialog tabs and the backend `/system_prompt` API). Uniqueness of `name` is
 * validated at the application layer (trim + case-insensitive), not by the database
 * (Dexie has no unique indexes).
 */
export interface PersonaPreset {
  /** Auto-increment primary key assigned by Dexie on insert */
  id?: number;
  /** Display name (stored trimmed, original case kept; duplicate check is case-insensitive) */
  name: string;
  /** Persona file contents keyed by exact basenames: 'AGENTS.md' | 'IDENTITY.md' | 'SOUL.md' | 'USER.md' */
  content: Record<string, string>;
  /** Creation time (epoch ms) */
  createdAt: number;
  /** Last content-update time (epoch ms) */
  updatedAt: number;
}

/** Primary key of the global pending profile in the character table (not a real session ID) */
export const GLOBAL_SESSION_KEY = '__global__';

/**
 * Built-in default character info (so callers can map it into a `CachedCharacter` snapshot).
 *
 * The default avatar/names are built into the frontend (see `defaultCharacter.ts`);
 * used as the fallback when the global profile row does not exist yet, or a session has
 * no snapshot yet.
 */
export const DEFAULT_CACHED_CHARACTER: Pick<CachedCharacter, 'userName' | 'userAvatar' | 'aiName' | 'aiAvatar'> = {
  userName: DEFAULT_CHARACTER.userName,
  userAvatar: DEFAULT_CHARACTER.userAvatar,
  aiName: DEFAULT_CHARACTER.aiName,
  aiAvatar: DEFAULT_CHARACTER.aiAvatar
};

class HistoryDb extends Dexie {
  /** Session message cache. */
  messages!: Table<CachedMessage, number>;
  /** Per-session character display info table (primary key session_id, including the global {@link GLOBAL_SESSION_KEY} row) */
  character!: Table<CachedCharacter, string>;
  /** Locally persisted session-list placeholder table (restores empty sessions created before a refresh that have no messages yet) */
  sessions!: Table<CachedSessionMeta, string>;
  /** Draft cache table for in-flight agent turns (primary key [session_id+turn_num], see {@link DraftTurn}) */
  drafts!: Table<DraftTurn, [string, number]>;
  /** User-uploaded chat-area background image table (primary key session_id, fixed to the global row {@link GLOBAL_SESSION_KEY}) */
  background!: Table<CachedBackground, string>;
  /** Cached subagent run records table (primary key run_id, see {@link CachedSubagentRun}) */
  subagentRuns!: Table<CachedSubagentRun, string>;
  /** Session custom-title override table (primary key session id, see {@link SessionTitleOverride}) */
  sessionTitles!: Table<SessionTitleOverride, string>;
  /** AI persona preset table (auto-increment primary key id; name uniqueness is validated at the application layer, see {@link PersonaPreset}) */
  personaPresets!: Table<PersonaPreset, number>;

  constructor() {
    super('ema-history-cache');
    this.version(1).stores({
      // Primary key id; compound index [session_id+turn_num] for per-session queries and deduplication.
      // Single-column session_id is used to clear a session's cache.
      messages: 'id, [session_id+turn_num], session_id'
    });
    this.version(2).stores({
      // Caches each session's avatar/name snapshot keyed by session_id (including the global row).
      character: 'session_id'
    });
    this.version(3).stores({
      // Local session-list placeholders (primary key is the session id); adding a table does not break existing table structures.
      sessions: 'id, updatedAt'
    });
    this.version(4).stores({
      // Compound primary key [session_id+turn_num]: whole-turn drafts are read/written by session + turn.
      // Single-column session_id clears all drafts for a session by prefix when deleting it.
      drafts: '[session_id+turn_num], session_id'
    });
    this.version(5).stores({
      // Global background image (primary key session_id=GLOBAL_SESSION_KEY); adding a table does not break existing table structures.
      background: 'session_id'
    });
    this.version(6)
      .stores({
        // Structure unchanged; only the messages table gains model_name/input_tokens/output_tokens columns.
        // Old rows default to null and can simply be treated as undefined when read.
        messages: 'id, [session_id+turn_num], session_id'
      })
      .upgrade(tx => {
        return tx
          .table('messages')
          .toCollection()
          .modify(msg => {
            if (msg.model_name === undefined) msg.model_name = null;
            if (msg.input_tokens === undefined) msg.input_tokens = null;
            if (msg.output_tokens === undefined) msg.output_tokens = null;
          });
      });
    this.version(7).stores({
      // Subagent run records cache (primary key run_id); adding a table does not break existing table structures.
      subagentRuns: 'run_id'
    });
    this.version(8).stores({
      // Session custom-title overrides (primary key is the session id); adding a table does not break existing table structures.
      // Separate from the sessions placeholder table: the placeholder table is cleared when a session
      // is promoted to a server-side session, while custom titles must survive promotion.
      sessionTitles: 'id'
    });
    this.version(9).stores({
      // AI persona presets (auto-increment primary key id; indexes support listing by creation
      // time and by name); adding a table does not break existing table structures.
      personaPresets: '++id, name, createdAt, updatedAt'
    });
  }
}

/**
 * Global unique Dexie database instance (used to cache conversation history).
 */
export const db = new HistoryDb();

/**
 * Merge server-returned message rows into the cache (deduplicated by `id`).
 *
 * @param rows  Message records returned by the server (including the `id` field)
 * @returns     Promise, resolves when the write completes
 */
export async function cacheMessages(rows: CachedMessage[]): Promise<void> {
  if (!rows || rows.length === 0) return;
  await db.messages.bulkPut(rows);
}

/**
 * Read all messages of a session from the local cache, sorted by `turn_num` ascending then `id` ascending.
 *
 * Uses a prefix query on the compound index `[session_id+turn_num]`: Dexie returns results
 * ordered ascending by the compound `(session_id, turn_num)`, avoiding a second sort in JS.
 *
 * @param sessionId Session ID
 * @returns         Array of the session's cached messages
 */
export async function readCachedMessages(sessionId: string): Promise<CachedMessage[]> {
  return await db.messages.where('[session_id+turn_num]').between([sessionId, MIN_KEY], [sessionId, MAX_KEY]).toArray();
}

/**
 * Compute a session's maximum turn number in the local cache.
 *
 * The compound index `[session_id+turn_num]` returns the session's rows ordered by
 * `turn_num` ascending, so the max `turn_num` can be taken from the last record,
 * avoiding iterating all rows in JS.
 *
 * Returns `0` when the cache is empty (the backend requires `min_turn_num >= 1`;
 * the client does not send an upper bound for this value, leaving that to server logic).
 *
 * @param sessionId Session ID
 * @returns         Max cached turn_num (0 when nothing is cached)
 */
export async function cachedMaxTurnNum(sessionId: string): Promise<number> {
  const last = await db.messages
    .where('[session_id+turn_num]')
    .between([sessionId, MIN_KEY], [sessionId, MAX_KEY])
    .last();
  return last ? last.turn_num : 0;
}

/**
 * Clear all messages of a session from the local cache.
 *
 * @param sessionId Session ID
 */
export async function clearCachedSession(sessionId: string): Promise<void> {
  await db.messages.where('session_id').equals(sessionId).delete();
}

/**
 * Write (cache / overwrite) a session's character display info snapshot.
 *
 * @param char Character info containing `session_id` (a real session ID or `GLOBAL_SESSION_KEY`)
 */
export async function cacheCharacter(char: CachedCharacter): Promise<void> {
  await db.character.put(char);
}

/**
 * Read a session's character display info snapshot (returns `undefined` when no record exists).
 *
 * @param sessionId Session ID or `GLOBAL_SESSION_KEY`
 */
export async function readCachedCharacter(sessionId: string): Promise<CachedCharacter | undefined> {
  return await db.character.get(sessionId);
}

/**
 * Clear a session's character display info snapshot (cleaned up when the session is deleted).
 *
 * @param sessionId Session ID (a real session; should not be `GLOBAL_SESSION_KEY`)
 */
export async function clearCachedCharacter(sessionId: string): Promise<void> {
  await db.character.delete(sessionId);
}

/**
 * Write (cache / overwrite) a local session placeholder entry.
 *
 * When an empty session is created (no messages sent yet) the server has no record; this
 * table keeps it alive across refreshes / restarts.
 *
 * @param meta Session placeholder entry (`id` is the session ID)
 */
export async function cacheSessionMeta(meta: CachedSessionMeta): Promise<void> {
  await db.sessions.put(meta);
}

/**
 * Read all session placeholder entries from the local cache, sorted by `updatedAt` descending (newest first).
 *
 * @returns Locally cached session placeholder entries
 */
export async function readCachedSessionMetaList(): Promise<CachedSessionMeta[]> {
  const list = await db.sessions.toArray();
  return list.sort((a, b) => b.updatedAt - a.updatedAt);
}

/**
 * Delete a session's placeholder entry from the local cache (cleaned up when the session is deleted;
 * no need to keep the placeholder once a server-side record exists).
 *
 * @param sessionId Session ID
 */
export async function clearCachedSessionMeta(sessionId: string): Promise<void> {
  await db.sessions.delete(sessionId);
}

/**
 * Write (overwrite) a session's custom-title override.
 *
 * Called when the user renames a session inline in the left-hand list: the custom title is
 * persisted independently to the `sessionTitles` table, overrides the server-derived title
 * (the last user message) when loading the list, and is matchable by keyword filters.
 *
 * @param id    Session ID
 * @param title Custom title
 */
export async function saveSessionTitleOverride(id: string, title: string): Promise<void> {
  await db.sessionTitles.put({ id, title, updatedAt: Date.now() });
}

/**
 * Read all sessions' custom-title overrides.
 *
 * @returns Mapping of `session ID → custom title` (an empty Map when there are no rename records)
 */
export async function readSessionTitleOverrides(): Promise<Map<string, string>> {
  const rows = await db.sessionTitles.toArray();
  return new Map(rows.map(r => [r.id, r.title]));
}

/**
 * Delete a session's custom-title override (cleaned up when the session is deleted)
 *
 * @param id Session ID
 */
export async function clearSessionTitleOverride(id: string): Promise<void> {
  await db.sessionTitles.delete(id);
}

/**
 * Write (wholesale overwrite) a session's in-flight draft for one turn.
 *
 * On every state change (send / each tool phase / error / before persistence reconciliation)
 * the caller rewrites the row with the "current full `MessageItem[]`", achieving
 * "cache on every step".
 *
 * @param draft The whole-turn draft to persist (keyed by `session_id` and `turn_num`; entries in
 *              `.messages` keep their local negative temporary ids so reconciliation can match
 *              and replace them by content)
 */
export async function saveDraftTurn(draft: DraftTurn): Promise<void> {
  await db.drafts.put(draft);
}

/**
 * Read all in-flight draft turns of a session from the local cache, sorted by `turn_num` ascending.
 *
 * The compound primary key `[session_id+turn_num]` returns the session's drafts in turn order;
 * `turn_num` shares the same source as live messages (positive turn numbers), so reconciliation
 * can match directly on `session_id + turn_num + role + content` and replace negative-temporary-id
 * rows, achieving deduplication.
 *
 * @param sessionId Session ID
 * @returns         Array of the session's unfinished draft turns (empty array means no drafts)
 */
export async function readDraftTurns(sessionId: string): Promise<DraftTurn[]> {
  return await db.drafts.where('[session_id+turn_num]').between([sessionId, MIN_KEY], [sessionId, MAX_KEY]).toArray();
}

/**
 * Clear a session's in-flight draft for one turn.
 *
 * Called after the server persists successfully (reconciled via onDone or re-fetching history)
 * or after the user actively stops / clears it, preventing drafts and persisted messages from
 * being rendered twice.
 *
 * @param sessionId Session ID
 * @param turnNum   The matched draft turn (positive turn number from the same source as live messages)
 */
export async function clearDraftTurn(sessionId: string, turnNum: number): Promise<void> {
  await db.drafts.delete([sessionId, turnNum]);
}

/**
 * Clear all in-flight draft turns of a session from the local cache.
 *
 * Cleaned up when the session is deleted, preventing orphan drafts from incorrectly
 * rehydrating after a session with the same id is recreated.
 *
 * @param sessionId Session ID
 */
export async function clearDraftSession(sessionId: string): Promise<void> {
  await db.drafts.where('session_id').equals(sessionId).delete();
}

/**
 * Write (overwrite) the global chat-area background image.
 *
 * @param backgroundUrl Background image base64 data URL (`data:image/...;base64,...`); empty string clears the background
 * @param backgroundOpacity Overlay opacity (0-100)
 */
export async function saveBackground(backgroundUrl: string, backgroundOpacity: number): Promise<void> {
  await db.background.put({
    session_id: GLOBAL_SESSION_KEY,
    backgroundUrl,
    backgroundOpacity
  });
}

/**
 * Read the global chat-area background config (returns `undefined` when not set).
 *
 * @returns Background config `{ backgroundUrl, backgroundOpacity }`; returns undefined when not set
 */
export async function readBackgroundConfig(): Promise<
  { backgroundUrl: string; backgroundOpacity: number } | undefined
> {
  const row = await db.background.get(GLOBAL_SESSION_KEY);
  if (!row?.backgroundUrl) return undefined;
  return {
    backgroundUrl: row.backgroundUrl,
    backgroundOpacity: row.backgroundOpacity ?? 0
  };
}

/**
 * Write one (or more) subagent run records into the local cache (deduplicated by `run_id`, overwrites supported too).
 *
 * Both the real-time events from `/subagents/ws` and the gap-filling `GET /subagents/runs`
 * calls go through this function, making IndexedDB the authoritative local data source for
 * the background task list.
 *
 * @param runs Subagent run records to cache (a single entry or an array)
 */
export async function cacheSubagentRuns(runs: CachedSubagentRun[]): Promise<void> {
  if (!runs || runs.length === 0) return;
  await db.subagentRuns.bulkPut(runs);
}

/**
 * Read all subagent run records from the local cache, sorted by spawn time descending (newest first).
 *
 * The result is sorted descending using the ordering implied by each record's `run_id`
 * (backend run_ids increase monotonically), so the UI can render the non-real-time task
 * list directly from the cache.
 *
 * @returns Array of locally cached subagent run records (empty array means no records)
 */
export async function readCachedSubagentRuns(): Promise<CachedSubagentRun[]> {
  const list = await db.subagentRuns.toArray();
  // run_id is the backend's incrementing sequence number; sorting numerically descending gives newest-first
  return list.sort((a, b) => {
    const an = Number(a.run_id);
    const bn = Number(b.run_id);
    if (Number.isFinite(an) && Number.isFinite(bn)) return bn - an;
    // Fall back to literal comparison for non-numeric ids (descending lexicographic order)
    return String(b.run_id) < String(a.run_id) ? -1 : 1;
  });
}

/**
 * Delete the subagent run records with the given `run_id`s from the local cache.
 *
 * Used for batch deletion in "background tasks": after the backend deletes a subtree
 * (root + descendants), the frontend deletes the corresponding records in Dexie/IndexedDB
 * based on that, fully cleaning the cache.
 *
 * @param runIds Array of run_ids to delete (including the root node and all its descendants)
 */
export async function deleteCachedSubagentRuns(runIds: string[]): Promise<void> {
  if (!runIds || runIds.length === 0) return;
  await db.subagentRuns.bulkDelete(runIds);
}

/**
 * Clear all subagent run records from the local cache.
 *
 * When the backend data is wiped (e.g. rebuilding the repository), the frontend uses this
 * to clear stale cache and avoid rendering zombie records.
 */
export async function clearCachedSubagentRuns(): Promise<void> {
  await db.subagentRuns.clear();
}

/**
 * List all persona presets, sorted by `createdAt` ascending (oldest first).
 *
 * @returns Array of persona presets in creation order
 */
export async function listPersonaPresets(): Promise<PersonaPreset[]> {
  return await db.personaPresets.orderBy('createdAt').toArray();
}

/**
 * Read one persona preset by its id (returns `undefined` when no record exists).
 *
 * @param id Persona preset id (Dexie auto-increment primary key)
 */
export async function getPersonaPreset(id: number): Promise<PersonaPreset | undefined> {
  return await db.personaPresets.get(id);
}

/**
 * Find a persona preset by name (trim + case-insensitive comparison against every row).
 *
 * Dexie indexes are case-sensitive, so name uniqueness must be validated at the
 * application layer: the given name and every stored name are compared as
 * `name.trim().toLowerCase()`.
 *
 * @param name Name to look up (compared after trim, case-insensitively)
 */
export async function findPersonaPresetByName(name: string): Promise<PersonaPreset | undefined> {
  const target = name.trim().toLowerCase();
  const rows = await db.personaPresets.toArray();
  return rows.find(row => row.name.trim().toLowerCase() === target);
}

/**
 * Create a persona preset with a unique name and return its new id.
 *
 * The name is stored trimmed (original case kept). Duplicate detection runs first via
 * {@link findPersonaPresetByName} (trim + case-insensitive); on a duplicate an `Error`
 * whose message contains "duplicate" is thrown, so callers can distinguish it from
 * infrastructure failures.
 *
 * @param name    Preset display name (stored trimmed)
 * @param content Persona file contents keyed by 'AGENTS.md' / 'IDENTITY.md' / 'SOUL.md' / 'USER.md'
 * @returns       Auto-increment id of the newly created preset
 */
export async function createPersonaPreset(name: string, content: Record<string, string>): Promise<number> {
  const trimmedName = name.trim();
  const existing = await findPersonaPresetByName(trimmedName);
  if (existing) {
    throw new Error(`Persona preset name duplicate: "${trimmedName}"`);
  }
  const now = Date.now();
  return await db.personaPresets.add({ name: trimmedName, content, createdAt: now, updatedAt: now });
}

/**
 * Overwrite a persona preset's content ("direct overwrite" semantics).
 *
 * Only `content` and `updatedAt` are written — the preset's `name` is never changed
 * (the name stays the identity of the preset; renaming is not supported).
 *
 * @param id      Persona preset id
 * @param content New persona file contents (keyed by 'AGENTS.md' / 'IDENTITY.md' / 'SOUL.md' / 'USER.md')
 */
export async function updatePersonaPreset(id: number, content: Record<string, string>): Promise<void> {
  await db.personaPresets.update(id, { content, updatedAt: Date.now() });
}

/**
 * Delete a persona preset by its id.
 *
 * @param id Persona preset id
 */
export async function deletePersonaPreset(id: number): Promise<void> {
  await db.personaPresets.delete(id);
}
