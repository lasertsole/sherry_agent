/**
 * KeepAlive activation + history-loading lifecycle of one session page.
 *
 * Each `[sid].vue` instance is cached by KeepAlive under its own page-key and
 * BELONGS to one session (`mySid`). The global `route` singleton keeps changing
 * for every cached instance, so the rules here are:
 *  - first load: only this instance's own session history;
 *  - switched away (inactive): keep all in-memory state, never clear anything;
 *  - switched back: restore as-is (a polluted/empty list reloads);
 *  - session deleted: abort this instance's stream and release its caches.
 *
 * The frozen `mySid` (not the live route value) decides "is this deletion
 * mine?".
 */
import { onActivated, onDeactivated, ref, watch } from 'vue';
import type { Ref } from 'vue';
import type { MessageItem } from '~/pages/home/type';
import type { ChatController } from './messages';

/** The composer methods this slice drives (the input box's exposed surface). */
export interface ActivationChatInput {
  clearHistory?: () => void;
}

/** Collaborators of the session activation slice. */
export interface SessionActivationDeps {
  /** This page instance's frozen session id. */
  mySid: string;
  /** Live session id of the page (route-derived). */
  sessionId: Ref<string>;
  /** The page's message list (single source of truth). */
  chatMessages: Ref<MessageItem[]>;
  /** Whether the page is currently streaming a generation. */
  isSending: Ref<boolean>;
  /** The ongoing send-generation controller. */
  activeAgentController: Ref<ChatController | null>;
  /** Input box draft (two-way bound to the composer). */
  draft: Ref<string>;
  /** HITL approval slice (resume abort + card clear on a session switch). */
  hitl: {
    abortResume: () => void;
    clearRequest: () => void;
  };
  /** The page's composer instance (null until mounted). */
  chatInput: () => ActivationChatInput | null | undefined;
  /** Drop the queue badge of a session that is being deleted. */
  clearQueueBadge: () => void;
  /** Clear the pending media selections (they must not leak across sessions). */
  clearMediaSelection: () => void;
  /** Load this session's locked character snapshot. */
  ensureSessionCharacter: (sid: string) => void;
  /** Refresh the "is this a branch child?" flag for a session. */
  refreshBranchLink: (sid: string) => void;
  /**
   * Fetch history + the running turn's snapshot in one round trip. The page
   * renders a chat skeleton while this promise is pending on an empty list.
   */
  syncHistory: (sid: string) => Promise<void> | void;
  /** Re-raise a still-pending approval card. */
  restorePendingHitl: (sid: string) => unknown;
  /** Initialize the session's shared stores (plan snapshot, TaskFlow board). */
  initStores: (sid: string) => void;
  /** Claim the find-in-conversation shortcut while this page is active. */
  registerFindShortcut: (handler: () => void) => void;
  /** Release it only if this page still owns it. */
  unregisterFindShortcut: (handler: () => void) => void;
  /** Open the conversation find bar (the page's ChatBox handle). */
  openFindInConversation: () => void;
}

/**
 * Create the session activation slice for one session page.
 * @param deps
 */
export function useSessionActivation(deps: SessionActivationDeps) {
  /**
   * Whether this instance is in 'active' state (false when hidden in the
   * KeepAlive cache). Cached instances keep running their watchers, so this is
   * what tells "switch away" apart from "a new session".
   */
  const isActive = ref(false);

  /**
   * The find-in-conversation chord action. Registered while this page is
   * active; the `isActive` guard keeps a stale cached page from answering.
   */
  const onFindShortcut = (): void => {
    if (!isActive.value || !deps.mySid) return;
    deps.openFindInConversation();
  };

  onActivated(() => {
    isActive.value = true;
    // Claim the find chord for THIS page: a cached sibling may have owned it.
    deps.registerFindShortcut(onFindShortcut);
    // When returning to this session, refresh the HITL card that may still be
    // pending approval (idempotent: early-return if a card already exists or a
    // resume is in flight).
    if (deps.mySid) void deps.restorePendingHitl(deps.mySid);
    // Pull the session's shared snapshots (idempotent singleton listeners).
    if (deps.mySid) deps.initStores(deps.mySid);
  });
  onDeactivated(() => {
    isActive.value = false;
    // Release the chord only if it is still ours (a newer page keeps its own).
    deps.unregisterFindShortcut(onFindShortcut);
  });

  /**
   * Whether this instance has already loaded history for 'its own session (mySid)'.
   * The first KeepAlive cached instance only loads history on **first** mount; when
   * switching back later it restores memory state as-is.
   */
  let mySidLoaded = false;

  /**
   * Whether this instance's history request is still in flight. The page only
   * renders the chat skeleton for an EMPTY list, so a KeepAlive restore (which
   * already holds its messages) never flashes one.
   */
  const isHistoryLoading = ref(false);
  /** Guards against an older request settling after a newer one started. */
  let historyLoadSeq = 0;

  /**
   * Load history for the specified session for this instance (clears local state, then rebuilds).
   * Only called on new sessions / exception fallback paths.
   * @param sid
   */
  const doLoadFor = (sid: string) => {
    // When switching to / first-loading a new sid, clear all session-scoped local
    // state first, then load this session's own history.
    deps.chatMessages.value = [];
    deps.isSending.value = false;
    // Discard the in-flight request controller left over from the previous session.
    deps.activeAgentController.value = null;
    deps.hitl.abortResume();
    // The previous session's pending-approval card / input draft / selected
    // images must not leak into the new session.
    deps.hitl.clearRequest();
    deps.draft.value = '';
    deps.clearMediaSelection();
    // Load this session's locked character snapshot (if none, lock using the global profile)
    deps.ensureSessionCharacter(sid);
    // "Am I a branch child?" — one lookup per session load (fail-open).
    void deps.refreshBranchLink(sid);
    // One round trip: history + the running turn's snapshot. Frames arriving
    // during the request are held and flushed after the snapshot is applied.
    const seq = ++historyLoadSeq;
    // A new session's history request resolves almost instantly with an empty
    // list; flashing the skeleton for it promises a load that never happened.
    // Only a request still pending after the grace period shows one.
    isHistoryLoading.value = false;
    const skeletonTimer = setTimeout(() => {
      if (seq === historyLoadSeq) isHistoryLoading.value = true;
    }, 200);
    void Promise.resolve(deps.syncHistory(sid)).finally(() => {
      clearTimeout(skeletonTimer);
      // A newer load may already own the flag; only the latest settles it.
      if (seq === historyLoadSeq) isHistoryLoading.value = false;
    });
    // Restore a possibly still-pending HITL interrupt card (session switch /
    // refresh / browser reopen / server restart).
    void deps.restorePendingHitl(sid);
    mySidLoaded = true;
  };

  // On first screen, load the current session's history messages and render the
  // merged list into the chat list.
  watch(
    deps.sessionId,
    sid => {
      if (!sid) return;

      // 1) First time (history not yet loaded for mySid): only load this
      //    instance's own session history. With `immediate: true` the watcher
      //    fires synchronously during setup when isActive is still false, so the
      //    first-load check cannot rely on isActive and falls back to mySidLoaded.
      if (!mySidLoaded) {
        doLoadFor(deps.mySid);
        return;
      }

      // 2) Cached and inactive (the user switched to another session): keep all
      //    in-memory state as-is, never perform destructive clearing.
      if (!isActive.value) return;

      // 3) Active, already loaded, and switched back to this very session:
      //    restore the in-memory state as-is. Hardening: a polluted or empty list
      //    reloads this session's history; otherwise this is an idempotent refresh.
      if (sid === deps.mySid) {
        const polluted = deps.chatMessages.value.some(m => m.session_id && m.session_id !== deps.mySid);
        if (deps.chatMessages.value.length === 0 || polluted) {
          doLoadFor(deps.mySid);
        } else {
          deps.ensureSessionCharacter(deps.mySid);
          void deps.restorePendingHitl(deps.mySid);
        }
        return;
      }

      // 4) Active but sessionId is not this session: the "moment of switching
      //    away" (the watcher fires before onDeactivated). Never treat it as a
      //    new session — that would write another session's messages into this
      //    instance; on switch-back (3) restores or reloads.
    },
    { immediate: true }
  );

  /**
   * Stream abort handling when the session is deleted (home/index.vue broadcasts
   * `SESSION_ABORT_STREAM_EVENT`): abort this instance's stream, drop the queue
   * badge, clear the composer's history cache and the session's orphan drafts.
   *
   * The comparison uses the frozen `mySid`, not the live `sessionId`: the latter
   * reads the global route and flips to another sid while this instance is
   * cached, which would make a real deletion miss.
   * @param deletedSid
   */
  const handleAbortStreamOnDelete = (deletedSid: unknown) => {
    if (deletedSid !== deps.mySid) return;
    if (deps.activeAgentController.value) {
      deps.activeAgentController.value.abort();
      deps.activeAgentController.value = null;
      deps.isSending.value = false;
    }
    // Session gone: drop the queue badge so it cannot linger on the cached instance.
    deps.clearQueueBadge();
    // Clear the composer's history cache / browse state in this KeepAlive slot.
    deps.chatInput()?.clearHistory?.();
    // Clear all ongoing draft turns of this session (no orphan drafts may
    // re-hydrate a later session that reuses the id).
    void clearDraftSession(deps.mySid);
  };

  return { isActive, isHistoryLoading, handleAbortStreamOnDelete };
}
