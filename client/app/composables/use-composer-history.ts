/**
 * Composer question-history browser (↑ / ↓ / Esc).
 *
 * The composer keeps the session's recently sent questions in memory (newest
 * last, at most `MAX_HISTORY`) and lets the user walk them with the arrow keys.
 * `-1` means "not browsing — the composer shows the live draft"; entering browse
 * mode snapshots that draft so Escape (or walking past the newest entry) puts it
 * back.
 *
 * The DOM write itself stays with the composer (`writeDraft`), because it must
 * place the caret and sync the controlled draft model.
 */
import { ref } from 'vue';
import type { Ref } from 'vue';

/** Max number of cached history questions browsable with the up/down arrow keys. */
export const MAX_COMPOSER_HISTORY = 10;

/** Collaborators of the history browser. */
export interface ComposerHistoryOptions {
  /** The controlled draft model. */
  draft: Ref<string>;
  /** Whether browsing is suspended (sending or the box is disabled). */
  blocked: () => boolean;
  /** Replace the composer's content with `text` (caret at the end). */
  writeDraft: (text: string) => void;
}

/**
 * Create the composer's question-history browser.
 * @param options
 */
export function useComposerHistory(options: ComposerHistoryOptions) {
  /**
   * In-memory history cache of user questions already sent in this session
   * (following the composer instance/session lifecycle, isolated per session by
   * KeepAlive). Stored in send order, newest at the end of the array.
   */
  const questionHistory = ref<string[]>([]);

  /**
   * Current browse position: an index into `questionHistory`; -1 means "not
   * browsing — the input box shows the user's own live draft".
   */
  const browseIndex = ref(-1);

  /**
   * Backup of the draft the user was editing before entering browse mode.
   * Restored when browse mode is exited via Escape.
   */
  let browsingSnapshot = '';

  /** ↑ key: browse older history questions. When the oldest entry is reached, return to the current draft (browseIndex = -1). */
  function handleKeyArrowUp(): void {
    if (options.blocked() || questionHistory.value.length === 0) return;

    // First ↑ press: enter browse mode, remember the current draft, start from the newest history entry
    if (browseIndex.value === -1) {
      browsingSnapshot = options.draft.value;
      browseIndex.value = questionHistory.value.length - 1;
    } else if (browseIndex.value > 0) {
      // Continue upward: hit an older entry
      browseIndex.value -= 1;
    } else {
      // Already at the oldest entry (index === 0); pressing ↑ again returns to the current draft
      browseIndex.value = -1;
    }

    const nextDraft =
      browseIndex.value === -1 ? browsingSnapshot : (questionHistory.value[browseIndex.value] ?? browsingSnapshot);
    options.writeDraft(nextDraft);
  }

  /** ↓ key: browse newer history questions. After returning to the latest draft (browseIndex = -1), further ↓ presses do nothing. */
  function handleKeyArrowDown(): void {
    if (options.blocked()) return;

    if (browseIndex.value === -1) return;
    // On the current draft (browseIndex already back to -1): keep as is
    if (browseIndex.value >= questionHistory.value.length - 1) {
      browseIndex.value = -1;
      options.writeDraft(browsingSnapshot);
      return;
    }
    // Move down to a newer history question
    browseIndex.value += 1;
    options.writeDraft(questionHistory.value[browseIndex.value] ?? browsingSnapshot);
  }

  /** Escape key: exit browse mode, restoring the user's draft from before browsing began. */
  function handleKeyEscape(): void {
    if (options.blocked()) return;
    if (browseIndex.value === -1) return;
    browseIndex.value = -1;
    options.writeDraft(browsingSnapshot);
  }

  /**
   * Stores a sent user question into the history cache (dedup, keeping at most
   * the most recent `MAX_COMPOSER_HISTORY` entries). Identical text is
   * deduplicated only at the end: if it already exists, remove it from its old
   * position and append it to the end, keeping the "newest" semantics correct.
   * @param text
   */
  function recordQuestion(text: string): void {
    if (!text) return;
    const withoutDup = questionHistory.value.filter(q => q !== text);
    withoutDup.push(text);
    // Over the limit: drop the oldest entry
    if (withoutDup.length > MAX_COMPOSER_HISTORY) withoutDup.splice(0, withoutDup.length - MAX_COMPOSER_HISTORY);
    questionHistory.value = withoutDup;

    // Sending exits browse mode, returning to the "live draft" state (the draft is already cleared at this point)
    browseIndex.value = -1;
    browsingSnapshot = '';
  }

  /**
   * Clears this session's history cache and browse state (the parent calls this
   * when a session is deleted, so a lingering KeepAlive slot cannot show the
   * deleted session's history later).
   */
  function clearHistory(): void {
    questionHistory.value = [];
    browseIndex.value = -1;
    browsingSnapshot = '';
  }

  return { handleKeyArrowUp, handleKeyArrowDown, handleKeyEscape, recordQuestion, clearHistory };
}
