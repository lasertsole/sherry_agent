/**
 * Session branch slice for one session page: "am I a branch child?" and
 * "branch from this answer".
 *
 * A branch is the side-chat fork machinery with its fork point parameterized —
 * a session of its own, not a right-sidebar panel. Creating it lives here;
 * NAVIGATING to the child stays with the page (it owns the router/locale path).
 */
import { ref } from 'vue';
import type { Ref } from 'vue';
import { useI18n } from 'vue-i18n';
import { logUtil } from '~/utils/log';
// Bridge modules are not auto-imported (same explicit-import convention as the
// composables that call them).
// eslint-disable-next-line @typescript-eslint/no-restricted-imports
import { fetchSessionLink } from '~/composables/bridge/session';

/** Collaborators of the session branch slice. */
export interface SessionBranchDeps {
  /** This page instance's frozen session id. */
  mySid: string;
  /** Whether the page is currently streaming a generation. */
  isSending: Ref<boolean>;
}

/**
 * Create the session branch slice.
 * @param deps
 */
export function useSessionBranch(deps: SessionBranchDeps) {
  const { t } = useI18n();

  /**
   * Whether THIS session is itself a fork (a side chat or a branch).
   *
   * The server refuses to fork a fork (no nested side chats), so the AI rows
   * hide the branch action here instead of offering an action that must fail.
   * An unreachable server reads as "root".
   */
  const isForkChild = ref(false);

  /**
   * Refresh the branch flag for a session (fail-open: unknown reads as root).
   * @param sid
   */
  async function refreshBranchLink(sid: string): Promise<void> {
    isForkChild.value = false;
    if (!sid) return;
    try {
      isForkChild.value = (await fetchSessionLink(sid)) !== null;
    } catch (e) {
      logUtil.w('[Branch] parent link lookup failed:', e);
    }
  }

  /**
   * Branch from one AI answer: create the child session at that turn.
   *
   * The page navigates to the returned id (a branch carries the parent's model,
   * thinking, directory and agent config — the server copies them).
   * @param turnNum The answered turn to fork at (1-based).
   * @returns The new child session id, or `null` when the branch was refused
   *   or failed (a toast has already reported the failure).
   */
  async function forkAtTurn(turnNum: number): Promise<string | null> {
    if (deps.isSending.value || isForkChild.value || !deps.mySid || turnNum <= 0) return null;
    try {
      const { sessionId: childSid } = await createSideChatSession(deps.mySid, '', turnNum);
      return childSid;
    } catch (e) {
      logUtil.e('[Branch] Failed to create the branch:', e);
      toastError(t('errors.forkFailed'), e instanceof Error ? e.message : String(e));
      return null;
    }
  }

  return { isForkChild, refreshBranchLink, forkAtTurn };
}
