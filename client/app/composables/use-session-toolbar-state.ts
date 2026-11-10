/**
 * Toolbar-derived state of the session page: the context-window usage the ring
 * shows, the running-tool-call mirror the terminal entry reads, the access-mode
 * refresh at turn boundaries, and the "reveal this row" focus action.
 *
 * All of it is DERIVED from the page's message list / streaming flag, so it is
 * kept out of the page's composition surface.
 */
import { computed, nextTick, watch } from 'vue';
import type { Ref } from 'vue';
import type { MessageItem } from '~/pages/home/type';
import { CHAT_ROLE } from '~/types/chat-role';

/** The running-commands store surface this slice drives (Pinia action). */
export interface RunningCommandsSurface {
  sync: (messages: MessageItem[] | undefined, isSending: boolean) => void;
}

/** The access-mode store surface this slice drives (Pinia action). */
export interface AccessModeSurface {
  hydrate: (sessionId: string) => unknown;
}

/** Collaborators of the toolbar-derived state. */
export interface SessionToolbarStateDeps {
  /** The page's message list (single source of truth). */
  chatMessages: Ref<MessageItem[]>;
  /** Whether the page is currently streaming a generation. */
  isSending: Ref<boolean>;
  /** Live session id of the page (route-derived). */
  sessionId: Ref<string>;
  /** Running-tool-call mirror of the terminal entry. */
  runningCommands: RunningCommandsSurface;
  /** Access-mode (shield) store refreshed at every turn boundary. */
  accessModeStore: AccessModeSurface;
  /** Open the sub-agent activity tab for a run. */
  openSubagentActivity: (runId: string) => void;
  /** Reveal one tool card in the transcript. */
  scrollToMessage: (messageId: number) => void;
}

/**
 * Create the toolbar-derived state slice of one session page.
 * @param deps
 */
export function useSessionToolbarState(deps: SessionToolbarStateDeps) {
  /**
   * Prompt size the provider reported for the newest finished turn — the
   * context the model actually saw. Drives the toolbar's usage ring before
   * (and between) the backend reads.
   */
  const lastPromptTokens = computed(() => {
    for (let i = deps.chatMessages.value.length - 1; i >= 0; i -= 1) {
      const message = deps.chatMessages.value[i];
      if (message && message.role === CHAT_ROLE.AI && message.inputTokens) return message.inputTokens;
    }
    return 0;
  });

  /**
   * Running tool calls of this session feed the toolbar's terminal entry: a TOOL
   * row is only reported while the session is generating (a finished turn — or a
   * turn paused on an approval card — owns nothing that is executing).
   */
  watch([deps.chatMessages, deps.isSending], ([messages, sending]) => deps.runningCommands.sync(messages, sending), {
    immediate: true,
    deep: false
  });

  /**
   * The shield's mode is DERIVED server-side — plan mode mirrors the plan status
   * — so a turn that enters or leaves plan mode changes it. Refresh at every
   * turn boundary: the same refresh covers a plan just approved (the status
   * flips to ready and the control falls back to the normal modes) and one the
   * agent entered by itself.
   */
  watch(deps.isSending, sending => {
    if (!sending && deps.sessionId.value) void deps.accessModeStore.hydrate(deps.sessionId.value);
  });

  /**
   * A row of the toolbar's terminal entry was picked: show where that work lives.
   *
   * A sub-agent opens its activity tab (live trajectory + summary + steer); a
   * terminal command reveals its own tool card in the chat, because the card is
   * where the command's output streams — no extra panel needed.
   * @param payload Which row was picked (`run` / `command`) and its id.
   * @param payload.kind
   * @param payload.id
   */
  const onTasksFocus = (payload: { kind: 'run' | 'command'; id: string | number }) => {
    if (payload.kind === 'run') {
      deps.openSubagentActivity(String(payload.id));
      return;
    }
    // The command's log is in the chat: reveal its card in place.
    void nextTick(() => deps.scrollToMessage(Number(payload.id)));
  };

  return { lastPromptTokens, onTasksFocus };
}
