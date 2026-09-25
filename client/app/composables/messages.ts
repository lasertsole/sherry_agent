import type { MultiModalMessage } from '@/types/message';
import type { Response } from '@/types/response';
import type { SessionRecord } from '@/pages/home/type';
import type {
  OnChunkCallback,
  OnDoneCallback,
  OnHitlCallback,
  OnQueuedCallback,
  HitlInterruptData,
  HitlResponse
} from './bridge';
import type { CachedMessage } from './db';
import { logUtil } from '~/utils/log';

/** One row of the `GET /sessions` list (server/trigger/http/messages.py). */
interface SessionListRow {
  session_id: string;
  last_time: string;
  title: string;
}

/**
 * Narrow guard for a JSON object payload (mirrors `ws-message.ts::isWsObjectFrame`,
 * for HTTP bodies): envelope fields must only be read off a real record.
 * @param value Any resolved payload value.
 */
function isJsonObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/**
 * Runtime guard for the pending-interrupt payload (audit #51 style).
 *
 * The server answers with the interrupt object itself, the legacy `{ data }`
 * envelope, or the literal text `"None"` (Python None) when nothing is pending.
 * Any other shape cannot produce a usable approval card, so it is rejected here
 * instead of being force-cast.
 * @param value Candidate payload (after envelope unwrapping).
 */
function isPendingInterrupt(value: unknown): value is HitlInterruptData {
  return isJsonObject(value) && value['None'] !== true;
}

/**
 * Controller returned by `postAgentStream`: a standard `AbortController` extended with
 * the HITL decision sender of the underlying stream (present only while the browser
 * WebSocket is still open; absent in Tauri mode).
 */
export interface ChatController extends AbortController {
  sendHitlResponse?: (response: HitlResponse) => void;
  /**
   * Client-generated protocol `msg_id` of this send (frozen protocol). Used by
   * the page to correlate `turn_started.message_ids` / `queued.message_id` back
   * to the local turn, so a batch of queued sends collapses into one reply.
   */
  msgId?: string;
}

/**
 * Event name for "abort streaming generation" when a session is deleted.
 *
 * When a session is deleted while its `[sid].vue` is still cached by KeepAlive (especially a
 * non-active session), its WebSocket streaming generation may still be running in the background,
 * continuously pushing content into the deleted session's chat state. The deleting side
 * (home/index.vue) broadcasts this event via mitt (the payload is the session id), and the
 * corresponding `[sid].vue` instance listens for it and aborts its AbortController to stop the
 * streaming generation.
 *
 * This event is broadcast purely within frontend memory; it triggers no server calls and
 * introduces no new dependencies.
 */
export const SESSION_ABORT_STREAM_EVENT = 'session:abort-stream';

/**
 * Request the conversation history (local Dexie cache first).
 *
 * On each request it:
 * 1. First reads the messages this session already has from the local cache, and takes the maximum
 *    `turn_num` in the cache as `min_turn_num` (only requesting new turns missing from the cache
 *    from the server);
 * 2. Merges the incremental messages returned by the server into the cache (deduplicated by message `id`);
 * 3. Returns the merged full list of "cache + increment".
 *
 * @param session_id Session ID
 * @param min_turn_num Minimum turn (>= 1; overridden by the cached max turn when a cache exists)
 * @param turn_page_size Turn page size (clamped to 1-200, mirroring the server cap)
 * @param turn_page_num Page number
 * @returns {Promise<CachedMessage[]>} Array of conversation history records (the raw local cache row structure)
 */
const MAX_TURN_PAGE_SIZE = 200;

export async function get_history_by_turn_page(
  session_id: string,
  min_turn_num: number,
  turn_page_size: number,
  turn_page_num: number
): Promise<CachedMessage[]> {
  const cached = await readCachedMessages(session_id);
  // Use the max turn_num of the existing local cache as min_turn_num,
  // requesting from the server only the newer turns missing from the cache;
  // but the caller-provided min_turn_num takes precedence (usable to override the cache max,
  // e.g. to load earlier history or a specified range).
  const cachedMinTurn = await cachedMaxTurnNum(session_id);
  // The server requires min_turn_num >= 1; when the cache is empty (no max turn), use the
  // caller-provided value, but still clamp it to >= 1 (0 would be rejected by the server's
  // Pydantic validation).
  const effectiveMinTurn = cachedMinTurn > min_turn_num ? cachedMinTurn : Math.max(min_turn_num, 1);

  try {
    const res = await fetchApi<CachedMessage[] | Response<CachedMessage[]>>({
      url: '/get_history_by_turn_page',
      opts: {
        session_id,
        min_turn_num: effectiveMinTurn,
        turn_page_size: Math.min(Math.max(turn_page_size, 1), MAX_TURN_PAGE_SIZE),
        turn_page_num
      },
      method: 'get'
    });
    // null = request failed (audit #53): fall back to the local cache, same as the catch below.
    if (res === null) return cached;

    // The server's /get_history_by_turn_page directly returns an array of message rows
    // (list[dict]), not a { data: [...] } wrapper object. Compatibility handling here: if the
    // response itself is an array, use it directly; otherwise fall back to reading res.data
    // (for the legacy wrapped format).
    const fetched: CachedMessage[] = Array.isArray(res) ? res : res.data || [];

    // Write to the cache (bulkPut deduplicates by the id primary key)
    await cacheMessages(fetched);

    return mergeDedup(cached, fetched);
  } catch {
    // When the server request fails, fall back to returning the local cache to guarantee offline availability
    return cached;
  }
}

/**
 * Merge the cached and server-returned messages for a session, deduplicate by `id`, and return
 * sorted by `turn_num` ascending.
 * @param cached
 * @param fetched
 */
function mergeDedup(cached: CachedMessage[], fetched: CachedMessage[]): CachedMessage[] {
  const seen = new Map<number, CachedMessage>();
  for (const m of cached) seen.set(m.id, m);
  for (const m of fetched) seen.set(m.id, m); // Server data overrides cache rows with the same id
  return [...seen.values()].sort((a, b) => a.turn_num - b.turn_num || a.id - b.id);
}

/**
 * Fetch one page of OLDER history for scroll-up pagination.
 *
 * Distinct from `get_history_by_turn_page`, whose `min_turn_num` resolves to
 * the local cache's MAX turn (an incremental-refresh contract that can never
 * reach older turns). The server pages BACKWARD FROM THE NEWEST TURN
 * (`page = 1` is the newest window; `min_turn_num` is only a lower clamp), so
 * this asks for the page whose window covers the turns just below
 * `before_turn` and returns only rows strictly older than it.
 *
 * @param session_id Session ID
 * @param before_turn Exclusive upper bound: fetch turns strictly below this number.
 * @param newest_turn The newest loaded turn (the server counts pages from it).
 * @param turn_page_size Turns per page (clamped to 1-200, mirroring the server cap).
 * @returns `{ rows, exhausted }` — rows ascending by turn ([] when the session
 *          start was reached), or `null` when the request FAILED (retryable;
 *          never treat a failure as "no more history").
 */
export async function get_older_history_page(
  session_id: string,
  before_turn: number,
  newest_turn: number,
  turn_page_size: number
): Promise<{ rows: CachedMessage[]; exhausted: boolean } | null> {
  const size = Math.min(Math.max(turn_page_size, 1), MAX_TURN_PAGE_SIZE);
  // The page whose window ends at (or just above) `before_turn - 1` while
  // covering it: floor keeps the window's tail overlapping the loaded range,
  // so no turn can fall into a gap between pages.
  const page = Math.max(Math.floor((newest_turn - before_turn + 1) / size), 0) + 1;
  try {
    const res = await fetchApi<CachedMessage[] | Response<CachedMessage[]>>({
      url: '/get_history_by_turn_page',
      opts: { session_id, min_turn_num: 1, turn_page_size: size, turn_page_num: page },
      method: 'get'
    });
    if (res === null) return null;
    const fetched: CachedMessage[] = Array.isArray(res) ? res : res.data || [];
    const older = fetched.filter(m => m.turn_num > 0 && m.turn_num < before_turn);
    if (!older.length) return { rows: [], exhausted: true };
    await cacheMessages(older);
    const turns = new Set(older.map(m => m.turn_num));
    return {
      rows: older.sort((a, b) => a.turn_num - b.turn_num || a.id - b.id),
      exhausted: turns.size < size || Math.min(...turns) <= 1
    };
  } catch {
    return null;
  }
}

/**
 * Clear session history
 * @param session_id Session ID
 * @returns {Promise<boolean>} Returns true when cleared successfully
 */
export async function clearSession(session_id: string): Promise<boolean> {
  try {
    await fetchApi({
      url: '/sessions',
      opts: { session_id },
      method: 'delete'
    });
    await clearCachedSession(session_id);
    return true;
  } catch {
    return false;
  }
}

/**
 * Fetch the full session list from the server, sorted by most recent activity descending.
 *
 * Corresponds to the server's GET /sessions (server/trigger/http/messages.py), which returns
 * ``[{session_id, last_time, title}]``. Here it is mapped to the frontend's ``SessionRecord``.
 *
 * @returns {Promise<SessionRecord[]>} Array of session records; an empty array is returned when the request fails
 */
export async function getSessionList(): Promise<SessionRecord[]> {
  try {
    const res = await fetchApi<SessionListRow[] | Response<SessionListRow[]>>({
      url: '/sessions',
      method: 'get'
    });
    // null = the request failed (audit #53): same fallback as the catch below.
    if (res === null) return [];
    // The server's /sessions directly returns an array, not a { data: [...] } wrapper object.
    // Compatibility handling here: if the response itself is an array, use it directly;
    // otherwise fall back to reading res.data.
    const rows: SessionListRow[] = Array.isArray(res) ? res : res.data || [];
    return rows.map(row => ({
      id: row.session_id,
      title: row.title ?? row.session_id,
      createTime: row.last_time
    }));
  } catch {
    // Return an empty list when the request fails, to avoid blocking the session list from loading
    return [];
  }
}

/**
 * Query whether the specified session has a pending HITL interrupt awaiting human approval.
 *
 * Corresponds to the server's GET /get_pending_interrupt (server/trigger/http/messages.py).
 * The server re-pushes `{tool_name, tool_args, description, allowed_decisions}` from the LangGraph
 * checkpoint, and returns null when there is no interrupt. Used to re-raise the pending approval
 * card after a session switch/page refresh/browser restart/server restart.
 *
 * @param session_id Session ID
 * @returns The pending HITL interrupt data; null when there is no interrupt or the request fails
 */
export async function getPendingInterrupt(session_id: string): Promise<HitlInterruptData | null> {
  try {
    const res = await fetchApi<HitlInterruptData>({
      url: '/get_pending_interrupt',
      opts: { session_id },
      method: 'get'
    });
    if (res == null) return null;
    // The server directly returns the interrupt object (or null) without a { data } wrapper;
    // compatibility handled here.
    const data = isJsonObject(res) ? (res['data'] ?? res) : res;
    // Compatibility fallback: the server may return the literal string "None" (Python None) as
    // text/plain, which ofetch will not JSON-parse; in that case data is a truthy string that must
    // be treated as "no interrupt", otherwise an invalid HITL card with an entirely empty tool_name
    // pops up (notably triggered right away on an empty session).
    return isPendingInterrupt(data) ? data : null;
  } catch (error) {
    // When the request fails (the session may have been cleared / the backend is not running),
    // silently treat it as no interrupt and do not block chat.
    logUtil.w('[getPendingInterrupt] Failed to query pending approval interrupt:', error);
    return null;
  }
}

/**
 * Stream the AI reply via the unified streaming pathway
 * (corresponds to server/trigger/ws/messages.py `/sessions/agent/ws`)
 *
 * In browser mode, streaming chunks are received over the WebSocket; in Tauri mode, via IPC +
 * Tauri Events. Decoupled from the old (now defunct) `/sessions/agent/sse` HTTP endpoint.
 *
 * @param session_id Session ID
 * @param multi_modal_message User input { text, image_base64_list?, audio_bytes_list?, video_bytes_list? }
 * @param onData Per-chunk text callback (carries the semantic type: text / tool_start / tool_end)
 * @param onDone Stream-end callback
 * @param onError Error callback
 * @param onHitl HITL interrupt callback
 * @param onQueued Queued callback (backend enqueued the message because the session is busy)
 * @param msgId Optional client-generated protocol `msg_id` (a UUID is generated when omitted)
 * @returns {ChatController} The caller can abort the request via controller.abort()
 */
export function postAgentStream(
  session_id: string,
  multi_modal_message: MultiModalMessage,
  onData: OnChunkCallback,
  onDone?: OnDoneCallback,
  onError?: (err: unknown) => void,
  onHitl?: OnHitlCallback,
  onQueued?: OnQueuedCallback,
  msgId?: string
): ChatController {
  const controller: ChatController = new AbortController();
  let stopFn: (() => void) | null = null;

  // Frozen protocol: every send carries a client-generated `msg_id`. The server
  // echoes it on `queued` and lists it in `turn_started` / `done` / `error` /
  // `stopped`, letting the persistent socket resolve the right pending send.
  const resolvedMsgId = msgId ?? generateMsgId();
  controller.msgId = resolvedMsgId;

  // Bridge to the unified streaming entry of bridge (browser WS / Tauri IPC).
  const { controller: stream, promise } = streamChatMessage(
    {
      session_id,
      msg_id: resolvedMsgId,
      origin: 'user',
      text: multi_modal_message.text ?? '',
      image_base64_list: multi_modal_message.image_base64_list,
      audio_bytes_list: multi_modal_message.audio_bytes_list,
      video_bytes_list: multi_modal_message.video_bytes_list
    },
    onData,
    onHitl,
    onDone,
    onQueued
  );
  stopFn = () => stream.abort();
  controller.sendHitlResponse = stream.sendHitlResponse ?? undefined;

  // User-initiated abort → trigger the stream stop
  controller.signal.addEventListener('abort', () => stopFn?.());

  promise
    .then(() => {
      // onDone is uniformly triggered by streamChatMessage when the stream ends normally
      // (carrying model metadata); it is not called again here, to avoid the callback firing
      // twice in browser WS mode.
    })
    .catch(err => {
      // A user-initiated abort (abort/stop) is not a business error and does not trigger onError
      const message = err instanceof Error ? err.message : String(err);
      if (message === 'aborted') {
        controller.abort();
        return;
      }
      onError?.(err);
    });

  return controller;
}

/** Generate a protocol `msg_id` (RFC4122 v4 UUID, with a non-crypto fallback). */
function generateMsgId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return `msg-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}
