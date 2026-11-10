/**
 * Composer edit mode for one session page: the "rewrite that sent user message"
 * state, its prefill/cancel controls and the send dispatch that routes to
 * either an edit or a normal send.
 *
 * Edit mode never touches the transcript itself — the page hands the edited
 * turn to its stream slice (`handleEdit`), which performs the truncate +
 * re-run.
 */
import { onUnmounted, ref } from 'vue';
import type { Ref } from 'vue';
import type { HitlRequestData, MessageItem } from '~/pages/home/type';

/** The composer methods this slice drives (the input box's exposed surface). */
export interface EditModeChatInput {
  setDraftText?: (text: string) => void;
}

/** Collaborators of the edit-mode slice. */
export interface SessionEditModeDeps {
  /** Input box draft (two-way bound to the composer). */
  draft: Ref<string>;
  /** Whether the page is currently streaming a generation. */
  isSending: Ref<boolean>;
  /** The pending approval request (edit is refused while one is open). */
  hitlRequest: Ref<HitlRequestData | null>;
  /** The page's composer instance (null until mounted). */
  chatInput: () => EditModeChatInput | null | undefined;
  /** Send a brand-new message (the page's stream slice). */
  onSend: (text: string) => void;
  /** Rewrite the given turn at `rowId` and re-run it (the page's stream slice). */
  onEdit: (turnNum: number, text: string, rowId: number) => void;
}

/**
 * Create the composer edit-mode slice for one session page.
 * @param deps
 */
export function useSessionEditMode(deps: SessionEditModeDeps) {
  /**
   * Edit mode: the turn being rewritten (`null` = the composer sends normally).
   * The row id travels with it so the backend truncates at the EXACT message
   * rather than guessing from the turn number.
   */
  const editingTurn = ref<{ turnNum: number; rowId: number } | null>(null);

  /**
   * Enter edit mode for a sent user message: prefill the composer and focus it.
   * @param message The clicked row.
   */
  const onEditMessage = (message: MessageItem) => {
    if (deps.isSending.value || deps.hitlRequest.value) return;
    editingTurn.value = { turnNum: message.turn_num, rowId: message.id };
    deps.chatInput()?.setDraftText?.(message.content);
  };

  /** Leave edit mode without touching the transcript. */
  const cancelEditMessage = () => {
    editingTurn.value = null;
    deps.draft.value = '';
    deps.chatInput()?.setDraftText?.('');
  };

  /**
   * Composer submit: in edit mode the text rewrites that turn (truncate + re-run),
   * otherwise it is a normal send.
   * @param text The composer's text (chips already extracted).
   */
  const onComposerSend = (text: string) => {
    const editing = editingTurn.value;
    if (!editing) {
      deps.onSend(text);
      return;
    }
    editingTurn.value = null;
    deps.onEdit(editing.turnNum, text, editing.rowId);
  };

  // Esc leaves edit mode (never while a turn streams: the composer is read-only
  // then, and its own Esc handling must stay untouched).
  const onEditModeKeydown = (event: KeyboardEvent) => {
    if (!editingTurn.value || event.key !== 'Escape') return;
    cancelEditMessage();
  };

  if (typeof window !== 'undefined') {
    window.addEventListener('keydown', onEditModeKeydown);
    onUnmounted(() => window.removeEventListener('keydown', onEditModeKeydown));
  }

  return { editingTurn, onEditMessage, cancelEditMessage, onComposerSend };
}
