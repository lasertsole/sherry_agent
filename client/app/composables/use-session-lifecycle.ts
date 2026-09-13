/**
 * Session lifecycle slice for one session page: character snapshot management
 * (global profile lock per session) and history loading with draft hydration.
 */
import { ref } from 'vue';
import type { Ref } from 'vue';
import type { MessageItem } from '../pages/home/type';
import type { CachedCharacter } from './db';
import { logUtil } from '~/utils/log';

/**
 * Create the session lifecycle slice for one session page.
 * @param chatMessages The page's message list (single source of truth; history merges into it)
 */
export function useSessionLifecycle(chatMessages: Ref<MessageItem[]>) {
  /**
   * Character display information (source is local Dexie session cache snapshot, see `CachedCharacter` in `db.ts`).
   * - `userAvatar` / `aiAvatar` are base64 data URL (user custom) or `/avatar/xxx.jpg` relative URL (built-in default), both can be directly rendered by `<img>`.
   * - Refreshed from corresponding session snapshot (or global pending profile) on each session switch/new creation, old sessions retain their own snapshots.
   */
  const characterInfo = ref<{ userName: string; userAvatar: string; aiName: string; aiAvatar: string }>({
    userName: DEFAULT_CACHED_CHARACTER.userName,
    userAvatar: DEFAULT_CACHED_CHARACTER.userAvatar,
    aiName: DEFAULT_CACHED_CHARACTER.aiName,
    aiAvatar: DEFAULT_CACHED_CHARACTER.aiAvatar
  });

  /** Default character display info (built-in: Touno Hanna / Sherry Orange + default avatar URLs, see `defaultCharacter.ts`) */
  const defaultCharacter = (): { userName: string; userAvatar: string; aiName: string; aiAvatar: string } => ({
    userName: DEFAULT_CACHED_CHARACTER.userName,
    userAvatar: DEFAULT_CACHED_CHARACTER.userAvatar,
    aiName: DEFAULT_CACHED_CHARACTER.aiName,
    aiAvatar: DEFAULT_CACHED_CHARACTER.aiAvatar
  });

  /**
   * Map a character snapshot to `characterInfo` (empty segments fall back to built-in defaults).
   * @param snap
   */
  const applyCharacterSnapshot = (snap?: Pick<CachedCharacter, 'userName' | 'userAvatar' | 'aiName' | 'aiAvatar'>) => {
    const defaultInfo = defaultCharacter();
    characterInfo.value = snap
      ? {
          userName: snap.userName?.trim() ? snap.userName : defaultInfo.userName,
          userAvatar: snap.userAvatar ?? defaultInfo.userAvatar,
          aiName: snap.aiName?.trim() ? snap.aiName : defaultInfo.aiName,
          aiAvatar: snap.aiAvatar ?? defaultInfo.aiAvatar
        }
      : defaultInfo;
  };

  /**
   * Ensure the specified session has locked its own character snapshot and update `characterInfo` to that session's display info.
   *
   * Naming logic: System configuration - character configuration edits the 'global pending profile' (`GLOBAL_SESSION_KEY` row).
   * When each session is first opened, copy and lock the current global profile to its own `session_id` row;
   * Subsequent global updates (avatar/name changes) no longer affect old sessions with locked snapshots, only new sessions get the latest global values.
   *
   * @param sessionId Session ID
   */
  const ensureSessionCharacter = async (sessionId: string) => {
    try {
      const [globalSnap, sessionSnap] = await Promise.all([
        readCachedCharacter('__global__'),
        readCachedCharacter(sessionId)
      ]);
      // Session already has snapshot (old session locked avatar/name) → use snapshot directly, not affected by global changes.
      if (sessionSnap) {
        applyCharacterSnapshot(sessionSnap);
        return;
      }
      // Session has no snapshot yet (new session or never opened before) → use global profile snapshot and lock it.
      // Note: `base` might be the global row (with session_id=GLOBAL_SESSION_KEY),
      // must use `...base` then explicitly override session_id, avoid writing real session key into global row.
      const base = globalSnap ?? defaultCharacter();
      const locked: CachedCharacter = { ...base, session_id: sessionId };
      await cacheCharacter(locked);
      applyCharacterSnapshot(locked);
    } catch (error) {
      // On Dexie read/write exceptions, preserve current display and don't block chat.
      logUtil.w('[ensureSessionCharacter] 读取角色快照失败：', error);
    }
  };

  /**
   * Load history messages for specified session (local cache first, backend merges server-side increments),
   * merged into `chatMessages` (deduplicated by id), for ChatBox rendering.
   *
   * Fix: No longer reconstruct entire `currentSession.value` (that would overwrite user's already-sent local messages,
   * causing 'list cleared after sending'). Only merge history rows into single list, existing messages preserved.
   * @param sessionId
   */
  const loadSessionHistory = async (sessionId: string) => {
    const rows = await get_history_by_turn_page(sessionId, 0, 10, 1);
    const historyItems = toMessageItems(rows);

    // Merge and deduplicate: existing ids preserve local versions (including unsent temporary messages with negative ids),
    // server real ids are added as-is. Overall turn_num ascending ensures stable order.
    //
    // Race condition fix: unsent temporary messages have negative ids (handleSend assigns large negative),
    // when server later returns the real positive id row for the same message, their ids differ, deduplication by id would preserve both
    // 'temporary negative id copy' and 'server positive id row', causing the same message to render twice.
    //
    // Therefore for each local negative id temporary copy, directly match its real positive id in server history rows by
    // 'same session + same turn_num + same role + same content' exact match;
    // if hit, replace with server row (discard temporary copy). Note cannot only merge find by (session, turn, role)
    // —— multiple same-role rows may appear in same turn (e.g. tool call + final reply within one AI round,
    // add_messages writes the whole batch into same turn_num), merge keys would lose some rows. Line-by-line exact match
    // on content ensures no cross-row mistaken replacement.
    const mergedById = new Map<number, MessageItem>();
    // Pre-index the server rows by their logical key (first occurrence wins, mirroring the previous Array.find)
    const serverRowByLogicalKey = new Map<string, MessageItem>();
    for (const h of historyItems) {
      if (h.id < 0) continue;
      const key = `${h.session_id}|${h.turn_num}|${h.role}|${h.content}`;
      if (!serverRowByLogicalKey.has(key)) serverRowByLogicalKey.set(key, h);
    }
    for (const m of chatMessages.value) {
      // Local temporary negative id rows: if server has already returned positive id row for same logical message, skip (use server row).
      if (m.id < 0) {
        const serverRow = serverRowByLogicalKey.get(`${m.session_id}|${m.turn_num}|${m.role}|${m.content}`);
        // Hit: replace temporary copy with server positive id row, add in subsequent loop; placeholder here to avoid duplication
        if (serverRow) {
          mergedById.set(serverRow.id, serverRow);
          continue;
        }
      }
      mergedById.set(m.id, m);
    }
    for (const h of historyItems) {
      // Only add when local doesn't have message with same id, to avoid overwriting content already updated during streaming
      if (!mergedById.has(h.id)) mergedById.set(h.id, h);
    }

    // —— Draft Hydration ——
    // Read incomplete draft turns for this session in IndexedDB (turns not persisted due to error/stop/HITL-reject, etc.,
    // and turns currently being streamed generated when server hasn't yet written back onDone results).
    //
    // Each draft message preserves its 'local negative temporary id' and 'positive turn_num'. Since positive turn_num matches real-time messages,
    // draft rows naturally appear after committed messages in same turn (see sorting at end), won't float before committed turns like old negative turn scheme.
    // And exact match with server/local collection by (session, turn, role, content) — if same logical message is already persisted (serverRowFor hit)
    // or already exists in local collection, skip this draft row to avoid duplicate rendering of drafts and real-time messages in same turn.
    const drafts = await readDraftTurns(sessionId);
    //
    // Draft 'Stale Turn' Judgment: If same turn_num already has 'server-persisted positive id row' in merged collection,
    // and contains a final AI result not in streaming (role=ai) — then this turn has been successfully persisted by server,
    // this draft is stale residue from when the turn was interrupted (e.g. Test: manually kill backend to let draft retain 'reply failed' marker,
    // then backend self-heals and writes back real content for same turn). At this point, exact match by (turn, role, content) line by line
    // would be missed due to different content (failed marker vs real reply), causing failed marker/failed tool rows and server real rows
    // to render together, duplicating the same turn. Correct approach: Any draft turn whose final AI result has been persisted should be skipped entirely, no longer hydrated.
    //
    // Only using server role=ai behavior as 'persisted final result' anchor is because normal ongoing streaming turns server
    // only writes human first (positive id), AI result not yet persisted, at this point draft AI rows should still be hydrated; only when AI is persisted
    // does it mean the turn is substantially complete and the draft must be stale.
    const staleDraftTurns = new Set<number>();
    // Pre-index the merged rows by (turn_num, role, content) for the draft dedup lookups below
    const mergedLogicalKeys = new Set<string>();
    for (const m of mergedById.values()) {
      if (m.id >= 0 && m.role === 'ai') staleDraftTurns.add(m.turn_num);
      mergedLogicalKeys.add(`${m.turn_num}|${m.role}|${m.content}`);
    }
    for (const draft of drafts) {
      for (const dm of draft.messages) {
        if (dm.session_id !== sessionId) continue; // Defensive: only hydrate this session
        // Stale turn: this turn has been persisted with final AI result by server, discard draft row entirely
        if (staleDraftTurns.has(dm.turn_num)) continue;
        // Whether draft row already exists in local collection / server history (matched by logical keys)
        if (mergedLogicalKeys.has(`${dm.turn_num}|${dm.role}|${dm.content}`)) continue;
        mergedById.set(dm.id, dm);
        mergedLogicalKeys.add(`${dm.turn_num}|${dm.role}|${dm.content}`);
      }
    }

    // Sort by turn_num ascending; within same turn, sort by id ascending (matches backend messages table
    // "ORDER BY turn_num ASC, id ASC"). Previously using id descending would reverse insertion order within same turn
    // (user message + AI reply share same turn_num), causing after refresh
    // AI replies to appear above user messages, last AI reply not at bottom.
    //
    // Draft rows use same positive turn_num + negative temporary id as real-time messages: for committed turns, draft id is negative,
    // real message id for that turn is positive, within same turn id ascending (negative < positive) drafts come first —— but same logical message
    // has been filtered out by 'skip' logic above, drafts that can be hydrated are all failed turns not yet persisted, thus won't
    // conflict with actual rendering.
    chatMessages.value = [...mergedById.values()].sort((a, b) => a.turn_num - b.turn_num || a.id - b.id);
  };

  return { characterInfo, applyCharacterSnapshot, ensureSessionCharacter, loadSessionHistory };
}
