/**
 * Transcript-selection actions for one session page: "add to this
 * conversation" (a quote chip in the composer) and "ask in a side chat" (create
 * the forked child session and open its right-sidebar tab).
 *
 * The child is parent-scoped, not tab-scoped — this slice only creates it; the
 * page decides where the tab opens.
 */
import { ref } from 'vue';
import { useI18n } from 'vue-i18n';
import { CHAT_ROLE } from '~/types/chat-role';
import type { SelectionPayload } from '~/composables/use-chat-selection';
import { logUtil } from '~/utils/log';

/** The composer methods this slice drives (the input box's exposed surface). */
export interface SelectionChatInput {
  addQuoteChip?: (text: string, source?: string) => void;
}

/** One side-chat tab open request (the page adds the parent session id). */
export interface SideChatOpenRequest {
  sessionId: string;
  inherited: boolean;
  quote: string;
  source: string;
}

/** Collaborators of the selection slice. */
export interface SessionSelectionDeps {
  /** This page instance's frozen session id. */
  mySid: string;
  /** The page's composer instance (null until mounted). */
  chatInput: () => SelectionChatInput | null | undefined;
  /** Open the side-chat tab for a freshly created child. */
  openSideChat: (request: SideChatOpenRequest) => void;
}

/**
 * Create the selection slice for one session page.
 * @param deps
 */
export function useSessionSelection(deps: SessionSelectionDeps) {
  const { t } = useI18n();

  /** True while a side session is being created (guards a double-click). */
  const creatingSideChat = ref(false);

  /**
   * The localized "from X" tooltip for a quote chip.
   * @param role The quoted message's role.
   */
  function selectionRoleLabel(role?: string): string {
    if (role === CHAT_ROLE.USER) return t('chat.selection.sourceUser');
    if (role === CHAT_ROLE.AI) return t('chat.selection.sourceAi');
    return t('chat.selection.sourceTool');
  }

  /**
   * "添加到当前会话" (add to this conversation): insert the selected text as a quote chip at the caret.
   * @param payload The quoted selection.
   */
  function onSelectionAdd(payload: SelectionPayload): void {
    if (!payload?.text) return;
    deps.chatInput()?.addQuoteChip?.(payload.text, selectionRoleLabel(payload.sourceRole));
  }

  /**
   * "在辅助会话中提问" (ask in a side session): create a normal session derived
   * from this one (same preset binding and character snapshot, no subagent
   * semantics) and hand it the quoted text.
   * @param payload The quoted selection.
   */
  async function onSelectionSideChat(payload: SelectionPayload): Promise<void> {
    if (!payload?.text || creatingSideChat.value || !deps.mySid) return;
    creatingSideChat.value = true;
    try {
      const { sessionId: childSid, inherited } = await createSideChatSession(
        deps.mySid,
        t('chat.selection.sideChatTitle')
      );
      deps.openSideChat({
        sessionId: childSid,
        inherited,
        quote: payload.text,
        source: selectionRoleLabel(payload.sourceRole)
      });
    } catch (e) {
      logUtil.e('[SideChat] Failed to create the side session:', e);
      toastError(t('chat.selection.sideChatFailed'), e instanceof Error ? e.message : String(e));
    } finally {
      creatingSideChat.value = false;
    }
  }

  return { creatingSideChat, selectionRoleLabel, onSelectionAdd, onSelectionSideChat };
}
