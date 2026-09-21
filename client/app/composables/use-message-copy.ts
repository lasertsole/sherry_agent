/**
 * Copy-to-clipboard behavior for chat messages (ChatBox).
 *
 * Extracted from `ChatBox.vue` with identical behavior:
 *  - `canCopyMessage` shows the button for user/AI text messages with a non-empty body;
 *  - the modern Clipboard API is preferred, with a hidden-textarea +
 *    `document.execCommand('copy')` fallback for non-secure/legacy contexts
 *    (a failure is only logged at warn level, never thrown to the template);
 *  - the "copied ✓" feedback is a single id + a 1500ms reset timer, cleared on
 *    re-click and on unmount, so rapid clicks only show the latest feedback.
 *
 * @module composables/use-message-copy
 */
import type { MessageItem } from '~/pages/home/type';
import { CHAT_ROLE } from '~/types/chat-role';
import { logUtil } from '~/utils/log';

/**
 * Write text to the clipboard.
 * Prefers the modern Clipboard API (requires a secure context); when it is unavailable or
 * rejects, fall back to "hidden textarea + document.execCommand('copy')". Returns false when
 * all paths fail; the caller only logs a warning.
 * @param text
 */
export async function copyTextToClipboard(text: string): Promise<boolean> {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // Clipboard API rejected/errored → use the fallback below
    }
  }
  return fallbackCopyText(text);
}

/**
 * Fallback copy: hidden textarea + execCommand('copy') (safety net for legacy environments / non-secure contexts)
 * @param text
 */
export function fallbackCopyText(text: string): boolean {
  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.setAttribute('readonly', '');
  // Positioned off-screen and invisible, avoiding layout jumps or flicker
  textarea.style.position = 'fixed';
  textarea.style.top = '-9999px';
  textarea.style.left = '-9999px';
  textarea.style.opacity = '0';
  document.body.appendChild(textarea);
  textarea.focus();
  textarea.select();
  let ok = false;
  try {
    ok = document.execCommand('copy');
  } catch {
    // execCommand may throw: keep ok=false, treating it as a copy failure
  }
  document.body.removeChild(textarea);
  return ok;
}

export function useMessageCopy() {
  /** Id of the message currently showing the "copied ✓" feedback (at most one at a time, preventing multiple bubbles from flashing simultaneously) */
  const copiedMessageId = ref<number | null>(null);

  /** Handle of the copy-feedback reset timer (must be cleared on re-click or component unmount, preventing an old timer from wiping the new feedback early) */
  let copyResetTimer: ReturnType<typeof setTimeout> | null = null;

  /**
   * Whether this message shows a copy button: only user/AI text messages with a non-empty body (tool cards and empty messages are excluded from the copy logic)
   * @param message
   */
  const canCopyMessage = (message: MessageItem): boolean => {
    return (
      (message.role === CHAT_ROLE.USER || message.role === CHAT_ROLE.AI) &&
      !!message.content &&
      message.content.trim().length > 0
    );
  };

  /**
   * Copy a message's raw Markdown body; on success briefly show the ✓ feedback (reverting to the copy icon after 1500ms)
   * @param message
   */
  const copyMessage = async (message: MessageItem) => {
    // Clear the previous feedback timer so that with rapid clicks only the latest feedback takes effect
    if (copyResetTimer) {
      clearTimeout(copyResetTimer);
      copyResetTimer = null;
    }
    const ok = await copyTextToClipboard(message.content ?? '');
    if (ok) {
      copiedMessageId.value = message.id;
      copyResetTimer = setTimeout(() => {
        copiedMessageId.value = null;
        copyResetTimer = null;
      }, 1500);
    } else {
      // Both paths failed: only warn, never throw to the template or interrupt rendering
      logUtil.w('[ChatBox] 复制消息正文失败，暂不支持剪贴板写入。');
    }
  };

  // Clear the pending feedback reset timer when the component unmounts
  onBeforeUnmount(() => {
    if (copyResetTimer) {
      clearTimeout(copyResetTimer);
      copyResetTimer = null;
    }
  });

  return { copiedMessageId, canCopyMessage, copyMessage };
}
