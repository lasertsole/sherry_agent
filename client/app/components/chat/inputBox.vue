<template>
  <div
    class="root"
    tabindex="-1">
    <div
      class="inputBox"
      :contenteditable="!sending && !disabled"
      :aria-disabled="sending || disabled"
      role="textbox"
      aria-multiline="true"
      :aria-label="t('chatInput.placeholder')"
      :placeholder="placeholderText"
      ref="inputDom"
      @input.stop="inputFunc($event)"
      @keydown.enter.stop.prevent="handleKeyEnter($event)"
      @keydown.up.stop.prevent="handleKeyArrowUp"
      @keydown.down.stop.prevent="handleKeyArrowDown"
      @keydown.escape.stop.prevent="handleKeyEscape"></div>

    <Button
      v-if="!sending"
      v-debounce:click.500="handleSend"
      :label="t('chatInput.send')"
      class="send"
      :disabled="!sendingAllowed || disabled" />
    <Button
      v-else
      v-debounce:click.300="() => emit('stop')"
      :label="t('chatInput.stop')"
      class="send"
      severity="danger" />
  </div>
</template>

<script lang="ts" setup>
import { computed, onMounted, watch, ref, type ShallowRef } from 'vue';
import { isEmpty } from 'lodash-es';
import { useI18n } from 'vue-i18n';
import { vDebounce } from '~/directives/debounce';

const { t } = useI18n();

/** Whether an AI reply is currently being generated (controlled by the parent to prevent duplicate sends) */
const props = withDefaults(defineProps<Props>(), {
  sending: false,
  disabled: false,
  disabledText: ''
});

interface Props {
  sending?: boolean;
  /** Whether input is forbidden (e.g. while a HITL request awaits approval, blocking manual input/sending) */
  disabled?: boolean;
  /** Placeholder text shown in the disabled state (e.g. "Waiting for approval…") */
  disabledText?: string;
}

const emit = defineEmits<{
  /** Send a message; the payload is plain text content */
  (e: 'send', text: string): void;
  /** Stop the current AI reply generation */
  (e: 'stop'): void;
}>();

/** Input area DOM */
const inputDom: ShallowRef<HTMLElement | null> = useTemplateRef('inputDom');

/**
 * Controlled draft content (two-way bound from the parent via `v-model:draft`).
 *
 * The parent (per-session ChatPage) writes the session's draft back when switching sessions,
 * so each session's unsent input content is preserved under KeepAlive caching.
 */
const draft = defineModel<string>('draft', { default: '' });

/** Whether sending is allowed (non-empty and not generating) */
const sendingAllowed = computed(() => !isEmpty(draft.value));

/** Input placeholder text: generating → "Thinking", disabled → the passed-in approval hint, otherwise the default hint */
const placeholderText = computed(() => {
  if (props.sending) return t('chatInput.thinking');
  if (props.disabled) return props.disabledText || t('chatInput.placeholder');
  return t('chatInput.placeholder');
});

/**
 * In-memory history cache of user questions already sent in this session (memory-level, following
 * this input box instance/session lifecycle, isolated per session by KeepAlive). Stored in send
 * order, newest at the end of the array, keeping at most the 10 most recent entries.
 */
const questionHistory = ref<string[]>([]);

/** Max number of cached history questions browsable with the up/down arrow keys */
const MAX_HISTORY = 10;

/**
 * Current browse position: an index into `questionHistory`; -1 means "not browsing — the input box
 * shows the user's own live draft". Pressing ↑ starts from the most recent entry (length-1) and
 * steps toward earlier ones one by one; pressing ↓ steps back toward newer ones. After passing the
 * earliest entry, it falls back to -1 (restoring the current draft).
 */
const browseIndex = ref(-1);

/**
 * Backup of the draft the user was editing before entering browse mode. Restored from this snapshot
 * when browse mode is exited via Escape; if the user edited the input box directly (inputFunc fired),
 * the snapshot is treated as abandoned and reset to the not-browsing state.
 */
let browsingSnapshot = '';

/**
 * Writes the given text into the input DOM and syncs the controlled draft (used to fill in a
 * "switched question" while browsing history with the up/down keys). Directly assigning `textContent`
 * loses the cursor and resets the existing content (full selection replaced), which matches the
 * "switch question" semantics — replacing the whole entry. autoSend is not triggered during browsing.
 */
function writeDraftToInput(text: string): void {
  if (inputDom.value) {
    inputDom.value.textContent = text;
    // Move the cursor to the end so the user can keep editing or press Enter to send directly
    try {
      const range = document.createRange();
      const sel = window.getSelection();
      range.selectNodeContents(inputDom.value);
      range.collapse(false);
      sel?.removeAllRanges();
      sel?.addRange(range);
      inputDom.value.focus();
    } catch {
      /* Ignore cursor positioning errors; they do not affect writing the text */
    }
  }
  draft.value = text;
}

/** ↑ key: browse older history questions. When the oldest entry is reached, return to the current draft (browseIndex = -1). */
function handleKeyArrowUp(): void {
  if (props.sending || props.disabled || questionHistory.value.length === 0) return;

  // First ↑ press: enter browse mode, remember the current draft, start from the newest history entry
  if (browseIndex.value === -1) {
    browsingSnapshot = draft.value;
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
  writeDraftToInput(nextDraft);
}

/** ↓ key: browse newer history questions. After returning to the latest draft (browseIndex = -1), further ↓ presses do nothing. */
function handleKeyArrowDown(): void {
  if (props.sending || props.disabled) return;

  if (browseIndex.value === -1) return;
  // On the current draft (browseIndex already back to -1): keep as is
  if (browseIndex.value >= questionHistory.value.length - 1) {
    browseIndex.value = -1;
    writeDraftToInput(browsingSnapshot);
    return;
  }
  // Move down to a newer history question
  browseIndex.value += 1;
  writeDraftToInput(questionHistory.value[browseIndex.value] ?? browsingSnapshot);
}

/** Escape key: exit browse mode, restoring the user's draft from before browsing began. */
function handleKeyEscape(): void {
  if (props.sending || props.disabled) return;
  if (browseIndex.value === -1) return;
  browseIndex.value = -1;
  writeDraftToInput(browsingSnapshot);
}

/** Input callback: strip contenteditable markup, keeping only plain text for validation */
function inputFunc(event: Event): void {
  if (!(event instanceof InputEvent)) {
    return;
  }

  const target = event.target as HTMLElement;
  const text = target.textContent ?? '';

  // When only blank lines/newlines remain, clear the inner content to avoid leftover <br>
  if (isEmpty(text) || text.trim() === '') {
    target.innerHTML = '';
    draft.value = '';
  } else {
    draft.value = text.trim();
  }
}

/** Enter sends (without Shift); Shift+Enter keeps the line break */
function handleKeyEnter(event: KeyboardEvent): void {
  if (event.shiftKey) return; // Shift+Enter inserts a line break
  handleSend();
}

/**
 * Stores a sent user question into the history cache (dedup, keeping at most the most recent MAX_HISTORY entries).
 * Identical text is deduplicated only at the end: if it already exists, remove it from its old position and append
 * it to the end, keeping the "newest" semantics correct.
 */
function recordQuestion(text: string): void {
  if (isEmpty(text)) return;
  const withoutDup = questionHistory.value.filter(q => q !== text);
  withoutDup.push(text);
  // Over the limit: drop the oldest entry
  if (withoutDup.length > MAX_HISTORY) withoutDup.splice(0, withoutDup.length - MAX_HISTORY);
  questionHistory.value = withoutDup;

  // Sending exits browse mode, returning to the "live draft" state (the draft is already cleared at this point)
  browseIndex.value = -1;
  browsingSnapshot = '';
}

/** Send: validate → record history → clear the input area → emit the text upward */
function handleSend(): void {
  if (!sendingAllowed.value || props.sending || props.disabled) return;
  const text = draft.value;
  if (isEmpty(text)) return;

  recordQuestion(text);
  clearInput();
  emit('send', text);
}

/** Clear the input area */
function clearInput(): void {
  if (inputDom.value) inputDom.value.innerHTML = '';
  draft.value = '';
}

/**
 * Syncs the external draft into the contenteditable input area.
 *
 * Called by the parent when switching sessions (KeepAlive restore) to render the session's cached
 * draft text back into the input box. Writes only when the input area is empty, to avoid
 * overwriting content the user is currently typing.
 */
function syncDraftToDom(): void {
  if (!inputDom.value) return;
  if (draft.value && inputDom.value.textContent !== draft.value) {
    inputDom.value.textContent = draft.value;
  }
}

// When the parent writes the draft via v-model:draft, sync it into the input DOM
watch(draft, () => syncDraftToDom());

// Sync once after the component mounts (onMounted also fires on KeepAlive restore)
onMounted(() => syncDraftToDom());

/**
 * Clears this session's history cache and browse state (for the parent to call when a session is deleted).
 *
 * After a session is deleted, its KeepAlive cache slot may still reside in memory (the slot is released
 * immediately only when the deleted session is the currently active one). If the slot lingers, this
 * inputBox's `questionHistory` remains with the cache, and the user would see the deleted session's
 * history the next time they manually visit that sid. The parent calls this method upon receiving the
 * `SESSION_ABORT_STREAM_EVENT` broadcast, so the history cache is cleared strictly in step with session deletion.
 */
function clearHistory(): void {
  questionHistory.value = [];
  browseIndex.value = -1;
  browsingSnapshot = '';
}

defineExpose({ clearHistory });
</script>

<style lang="scss" scoped>
@use 'sass:math';
@use '@/common.scss' as common;

.root {
  height: 100%;
  width: 100%;
  position: relative;
  // As a flex item of the parent (a fixed-height h-40 flex container), this must be shrinkable to keep the fixed height;
  // overflow hidden compounds the clipping, preventing excess content from pushing the outer page taller.
  display: flex;
  flex-direction: column;
  min-height: 0;
  overflow: hidden;

  > .inputBox {
    // Takes the parent's remaining height; min-height:0 allows shrinking, and combined with overflow-y:auto enables internal scrolling,
    // so no matter how much content there is, it only scrolls and never pushes the input area/page taller.
    flex: 1;
    min-height: 0;
    width: 100%;
    box-sizing: border-box;
    outline: none;
    word-break: break-all;
    padding: 0.5rem;
    overflow-y: auto;

    // contenteditable placeholder (implemented via a pseudo-element)
    &:empty::before {
      content: attr(placeholder);
      color: #9ca3af;
      pointer-events: none;
    }
  }

  > .send {
    position: absolute;
    right: 0.5rem;
    bottom: 0.5rem;
  }
}
</style>
