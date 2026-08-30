import type { MultiModalMessage } from '@/types/message';
import type { Response } from '@/types/response';
import type { SessionRecord } from '@/pages/home/type';
import {
  streamChatMessage,
  type OnChunkCallback,
  type OnDoneCallback,
  type OnHitlCallback,
  type HitlInterruptData
} from './bridge';
import { cacheMessages, cachedMaxTurnNum, clearCachedSession, readCachedMessages, type CachedMessage } from './db';

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
 * @param turn_page_size Turn page size
 * @param turn_page_num Page number
 * @returns {Promise<CachedMessage[]>} Array of conversation history records (the raw local cache row structure)
 */
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
    const res: Response = await fetchApi({
      url: '/get_history_by_turn_page',
      opts: {
        session_id,
        min_turn_num: effectiveMinTurn,
        turn_page_size,
        turn_page_num
      },
      method: 'get'
    });

    // The server's /get_history_by_turn_page directly returns an array of message rows
    // (list[dict]), not a { data: [...] } wrapper object. Compatibility handling here: if the
    // response itself is an array, use it directly; otherwise fall back to reading res.data
    // (for the legacy wrapped format).
    const fetched: CachedMessage[] = Array.isArray(res) ? (res as unknown as CachedMessage[]) : res.data || [];

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
 */
function mergeDedup(cached: CachedMessage[], fetched: CachedMessage[]): CachedMessage[] {
  const seen = new Map<number, CachedMessage>();
  for (const m of cached) seen.set(m.id, m);
  for (const m of fetched) seen.set(m.id, m); // Server data overrides cache rows with the same id
  return [...seen.values()].sort((a, b) => a.turn_num - b.turn_num || a.id - b.id);
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
    const res: Response = await fetchApi({
      url: '/sessions',
      method: 'get'
    });
    // The server's /sessions directly returns an array, not a { data: [...] } wrapper object.
    // Compatibility handling here: if the response itself is an array, use it directly;
    // otherwise fall back to reading res.data.
    const rows: Array<{ session_id: string; last_time: string; title: string }> = Array.isArray(res)
      ? (res as unknown as Array<{ session_id: string; last_time: string; title: string }>)
      : res.data || [];
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
    const res: Response = await fetchApi({
      url: '/get_pending_interrupt',
      opts: { session_id },
      method: 'get'
    });
    if (res == null) return null;
    // The server directly returns the interrupt object (or null) without a { data } wrapper;
    // compatibility handled here.
    const data = (res as unknown as { data?: unknown }).data ?? res;
    // Compatibility fallback: the server may return the literal string "None" (Python None) as
    // text/plain, which ofetch will not JSON-parse; in that case data is a truthy string that must
    // be treated as "no interrupt", otherwise an invalid HITL card with an entirely empty tool_name
    // pops up (notably triggered right away on an empty session).
    if (
      data == null ||
      typeof data !== 'object' ||
      Array.isArray(data) ||
      (data as Record<string, unknown>)['None'] === true
    ) {
      return null;
    }
    return data as HitlInterruptData;
  } catch (error) {
    // When the request fails (the session may have been cleared / the backend is not running),
    // silently treat it as no interrupt and do not block chat.
    console.warn('[getPendingInterrupt] 查询待审批中断失败：', error);
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
 * @returns {AbortController} The caller can abort the request via controller.abort()
 */
export function postAgentStream(
  session_id: string,
  multi_modal_message: MultiModalMessage,
  onData: OnChunkCallback,
  onDone?: OnDoneCallback,
  onError?: (err: unknown) => void,
  onHitl?: OnHitlCallback
): AbortController {
  const controller = new AbortController();
  let stopFn: (() => void) | null = null;

  // Bridge to the unified streaming entry of bridge (browser WS / Tauri IPC).
  const { controller: stream, promise } = streamChatMessage(
    {
      session_id,
      text: multi_modal_message.text ?? '',
      image_base64_list: multi_modal_message.image_base64_list,
      audio_bytes_list: multi_modal_message.audio_bytes_list,
      video_bytes_list: multi_modal_message.video_bytes_list
    },
    onData,
    onHitl,
    onDone
  );
  stopFn = () => stream.abort();
  const hitlSender = stream.sendHitlResponse ?? null;

  // Attach sendHitlResponse onto the returned AbortController
  (controller as any).sendHitlResponse = hitlSender;

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
