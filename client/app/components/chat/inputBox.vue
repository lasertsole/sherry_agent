<template>
  <div
    class="root"
    :class="{ 'drag-over': isDragOver }"
    tabindex="-1"
    @dragover.prevent="onDragOver"
    @dragleave.prevent="onDragLeave"
    @drop.prevent="onDrop">
    <div
      class="inputBox"
      :contenteditable="!disabled"
      :aria-disabled="disabled"
      role="textbox"
      aria-multiline="true"
      :aria-label="t('chatInput.placeholder')"
      :placeholder="placeholderText"
      ref="inputDom"
      @input.stop="inputFunc($event)"
      @keydown.stop="onComposerKeydown"></div>

    <div class="sendActions">
      <!-- Voice input: mic → record → transcribe → insert at the caret, fully
          inside this component (the composable is UI-free). Hidden while the
          box is disabled (a HITL card owns the input slot); disabled when STT
          is unconfigured (the status route's weights probe). While recording
          the button turns into the stop control, so it must stay clickable. -->
      <span
        v-if="voiceRecording"
        class="voiceTimer"
        >{{ voiceTimerLabel }}</span
      >
      <Button
        v-if="!disabled"
        v-debounce:click.300="toggleVoiceRecording"
        class="voice"
        :class="{ 'voice-recording': voiceRecording }"
        :icon="voiceIcon"
        text
        rounded
        :severity="voiceRecording ? 'danger' : 'secondary'"
        :aria-label="voiceAriaLabel"
        :title="voiceTooltip"
        :disabled="voiceStartBlocked && !voiceRecording" />
      <!-- Stop: only while generating; Send stays enabled/clickable while streaming —
          the backend queues the new message (queued frame) instead of rejecting it -->
      <Button
        v-if="sending"
        v-debounce:click.300="() => emit('stop')"
        :label="t('chatInput.stop')"
        class="stop"
        severity="danger" />
      <Button
        v-debounce:click.500="handleSend"
        :label="t('chatInput.send')"
        class="send"
        :title="sendTitle"
        :disabled="!sendingAllowed || disabled" />
    </div>
  </div>
</template>

<script lang="ts" setup>
import { computed, onMounted, watch, ref, type ShallowRef } from 'vue';
import { isEmpty } from 'lodash-es';
import { useI18n } from 'vue-i18n';
// Type-only: the mention helpers themselves are auto-imported composable
// exports (the repo's `no-restricted-imports` rule keeps value imports out of
// components).
import type { FileMentionPayload } from '~/composables/use-file-mention';
import type { VoiceErrorKind } from '~/composables/use-voice-input';

const { t } = useI18n();

/** Whether an AI reply is currently being generated (controls the stop button; sending while streaming stays allowed — the backend enqueues the new message) */
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
 * Voice input state machine (mic button: record → transcribe → insert).
 * `probeAvailability` runs once on mount; `null` (backend unreachable) keeps
 * the button clickable so the first attempt surfaces the real error.
 */
const voice = useVoiceInput();

const voiceRecording = computed(() => voice.state.value === 'recording');
const voiceTranscribing = computed(() => voice.state.value === 'transcribing');

/** The mic is blocked while a transcription runs or the box itself is disabled; unconfigured STT stays CLICKABLE — the click toasts the reason instead of silently refusing. */
const voiceStartBlocked = computed(() => props.disabled || voiceTranscribing.value);

const voiceIcon = computed(() =>
  voiceTranscribing.value ? 'pi pi-spin pi-spinner' : voiceRecording.value ? 'pi pi-stop-circle' : 'pi pi-microphone'
);

const voiceTimerLabel = computed(() => {
  const total = voice.elapsedSec.value;
  return `${String(Math.floor(total / 60)).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}`;
});

const voiceAriaLabel = computed(() => (voiceRecording.value ? t('chatInput.voiceStop') : t('chatInput.voiceRecord')));

const voiceTooltip = computed(() => {
  if (voice.isSttAvailable.value === false) return t('chatInput.voiceUnavailable');
  return voiceAriaLabel.value;
});

/** Toast-able failure kinds → the `chatInput.voice*` key that explains them. */
const VOICE_ERROR_KEYS: Record<VoiceErrorKind, string> = {
  permission: 'chatInput.voicePermissionDenied',
  unsupported: 'chatInput.voiceFailed',
  too_short: 'chatInput.voiceTooShort',
  warming: 'chatInput.voiceWarming',
  unavailable: 'chatInput.voiceUnavailable',
  failed: 'chatInput.voiceFailed'
};

/** Mic click: toggle record/stop; the stop half inserts the transcript at the caret. */
async function toggleVoiceRecording(): Promise<void> {
  if (voiceRecording.value) {
    const result = await voice.stopRecording();
    if (result.ok && result.text) {
      insertVoiceText(result.text);
    } else if (result.reason && result.reason !== 'idle') {
      toastWarn(t(VOICE_ERROR_KEYS[result.reason as VoiceErrorKind] ?? 'chatInput.voiceFailed'));
    }
    return;
  }
  const started = await voice.startRecording();
  if (!started && voice.lastError.value) {
    toastWarn(t(VOICE_ERROR_KEYS[voice.lastError.value] ?? 'chatInput.voiceFailed'));
  }
}

/** True while a workspace file is dragged over the composer (highlight hint). */
const isDragOver = ref(false);

/**
 * Highlight only for a workspace-file drag: an OS file drop is a different
 * feature (attachment upload) and must not light up this surface.
 * @param event
 */
function onDragOver(event: DragEvent): void {
  if (!event.dataTransfer?.types.includes(FILE_MENTION_MIME)) return;
  isDragOver.value = true;
  event.dataTransfer.dropEffect = 'copy';
}

/** The pointer left the composer: drop the highlight. */
function onDragLeave(): void {
  isDragOver.value = false;
}

/**
 * Drop a workspace file onto the composer: insert its mention chip at the
 * caret (never its content — the agent reads the file itself).
 * @param event
 */
function onDrop(event: DragEvent): void {
  isDragOver.value = false;
  const raw = event.dataTransfer?.getData(FILE_MENTION_MIME);
  if (!raw) return;
  try {
    const payload = JSON.parse(raw) as FileMentionPayload;
    if (payload?.path) insertFileMention(inputDom.value, payload);
  } catch {
    // A malformed payload (another app's drag) is ignored, not fatal.
  }
}

/**
 * Insert a mention chip programmatically (the file tree's "Add to chat",
 * relayed by the page as a mitt event). Also places the caret so the user can
 * keep typing right after the chip.
 * @param payload The referenced workspace entry.
 */
function addFileMention(payload: FileMentionPayload): void {
  if (props.disabled) return;
  insertFileMention(inputDom.value, payload);
}

/**
 * Insert a quoted-selection chip programmatically (the chat transcript's
 * right-click menu). The full text stays in the chip's attribute; the visible
 * label is only a preview.
 * @param text The selected text.
 * @param source A localized "from X" label for the chip's tooltip.
 */
function addQuoteChip(text: string, source?: string): void {
  if (props.disabled) return;
  insertQuoteChip(inputDom.value, text, source || t('chat.selection.quoteTooltip'));
}

/**
 * Controlled draft content (two-way bound from the parent via `v-model:draft`).
 *
 * The parent (per-session ChatPage) writes the session's draft back when switching sessions,
 * so each session's unsent input content is preserved under KeepAlive caching.
 */
const draft = defineModel<string>('draft', { default: '' });

/** Whether sending is allowed (non-empty draft; sending during an ongoing generation is allowed — the backend queues it) */
const sendingAllowed = computed(() => !isEmpty(draft.value));

/**
 * Input placeholder text. Disabled wins over everything (the box is not
 * editable then, so inviting the user to type would be a lie); otherwise a
 * running turn shows the queue hint — typing IS allowed and the message is
 * queued behind it — and the idle state shows the default hint.
 */
const placeholderText = computed(() => {
  if (props.disabled) return props.disabledText || t('chatInput.placeholder');
  if (props.sending) return t('chatInput.queueHint');
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
 * @param text
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

/**
 * Input callback: strip contenteditable markup, keeping only plain text for validation
 * @param event
 */
function inputFunc(event: Event): void {
  if (!(event instanceof InputEvent)) {
    return;
  }

  const target = event.target as HTMLElement;
  // The DRAFT is the plain-text projection of the composer: a mention chip
  // contributes the `@path` it carries, never its visible label — otherwise
  // the draft model (validation, history, cross-session restore) would hold a
  // string the user never meant to send.
  const text = extractTextForSend(target);

  // When only blank lines/newlines remain, clear the inner content to avoid leftover <br>
  if (isEmpty(text) || text.trim() === '') {
    target.innerHTML = '';
    draft.value = '';
  } else {
    draft.value = text.trim();
  }
}

/**
 * The composer's shortcut dispatch.
 *
 * The chords come from the shortcut table (rebindable in 菜单 → 系统设置), so
 * the element carries ONE handler instead of compile-time key modifiers: a
 * chord that matches is applied and its default prevented; everything else
 * falls through to the browser (typing, Shift+Enter's newline, caret movement
 * once the history chord has been rebound away from the arrows).
 * @param event
 */
function onComposerKeydown(event: KeyboardEvent): void {
  if (matchesBinding(event, 'send-message')) {
    event.preventDefault();
    handleKeyEnter(event);
    return;
  }
  if (matchesBinding(event, 'history-prev')) {
    event.preventDefault();
    handleKeyArrowUp();
    return;
  }
  if (matchesBinding(event, 'history-next')) {
    event.preventDefault();
    handleKeyArrowDown();
    return;
  }
  if (matchesBinding(event, 'cancel-input')) {
    event.preventDefault();
    handleKeyEscape();
  }
}

/** Send-button tooltip: the label plus the LIVE chord (follows a rebind). */
const sendTitle = computed(() => {
  const keys = effectiveKeys('send-message');
  return keys ? `${t('chatInput.send')} (${formatCombo(keys)})` : t('chatInput.send');
});

/**
 * Enter sends (without Shift); Shift+Enter keeps the line break
 * @param event
 */
function handleKeyEnter(event: KeyboardEvent): void {
  if (event.shiftKey) return; // Shift+Enter inserts a line break
  handleSend();
}

/**
 * Stores a sent user question into the history cache (dedup, keeping at most the most recent MAX_HISTORY entries).
 * Identical text is deduplicated only at the end: if it already exists, remove it from its old position and append
 * it to the end, keeping the "newest" semantics correct.
 * @param text
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

/** Send: validate → record history → clear the input area → emit the text upward.
 *  Gating note: `sending` no longer blocks the send — while the agent is streaming the backend
 *  enqueues the new message and reports it back as a `queued` frame (queue badge in [sid].vue). */
function handleSend(): void {
  if (!sendingAllowed.value || props.disabled) return;
  // The DRAFT is only the plain-text projection; mention chips carry their
  // `@path` in the DOM, so the send path reads the element itself.
  const text = extractTextForSend(inputDom.value).trim();
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
  if (draft.value && extractTextForSend(inputDom.value).trim() !== draft.value) {
    inputDom.value.textContent = draft.value;
  }
}

// When the parent writes the draft via v-model:draft, sync it into the input DOM
watch(draft, () => syncDraftToDom());

// Sync once after the component mounts (onMounted also fires on KeepAlive restore)
onMounted(() => {
  syncDraftToDom();
  // Voice availability is a one-shot probe; a backend that is down leaves
  // `null` (unknown) and the button stays clickable.
  void voice.probeAvailability();
});

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

/**
 * Insert transcribed voice text at the caret (plain text — no chip), with the
 * same flow as the mention chip's insertion: caret-in-composer range insert,
 * append-at-end fallback, then one synthetic input event so `inputFunc` syncs
 * the draft model. Newlines inside a transcript survive both the DOM (they are
 * part of the text node) and `extractTextForSend`.
 * @param text The transcript to drop into the composer.
 */
function insertVoiceText(text: string): void {
  if (props.disabled || !inputDom.value || !text) return;
  const node = document.createTextNode(text);
  const selection = window.getSelection();
  const range =
    selection && selection.rangeCount > 0 && inputDom.value.contains(selection.getRangeAt(0).commonAncestorContainer)
      ? selection.getRangeAt(0)
      : null;
  if (range) {
    range.deleteContents();
    range.insertNode(node);
    range.setStartAfter(node);
    range.collapse(true);
    selection?.removeAllRanges();
    selection?.addRange(range);
  } else {
    inputDom.value.appendChild(node);
  }
  inputDom.value.focus();
  inputDom.value.dispatchEvent(new InputEvent('input', { bubbles: true }));
}

defineExpose({
  clearHistory,
  addFileMention,
  addQuoteChip,
  insertVoiceText,
  // Edit mode prefills the composer and wants the caret ready: the same
  // write-and-focus path the ↑ history browser uses.
  setDraftText: writeDraftToInput
});
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

  // A workspace file is hovering the composer: the dashed outline names the
  // drop target (the mention chip is inserted at the caret on drop).
  &.drag-over > .inputBox {
    outline: 1px dashed var(--color-theme-main);
    outline-offset: -2px;
    background-color: color-mix(in srgb, var(--color-theme-main) 6%, transparent);
  }

  // One inline file reference: an atomic chip the caret skips over. The
  // visible label is the icon + file name; the text it stands for (`@path`)
  // lives in its `data-file-path` and is spliced back in on send.
  :deep(.file-mention-chip) {
    display: inline-flex;
    align-items: center;
    gap: 0.25rem;
    padding: 0 0.375rem;
    margin: 0 0.125rem;
    border-radius: 0.375rem;
    background-color: color-mix(in srgb, var(--color-theme-main) 12%, transparent);
    color: var(--color-theme-main);
    font-size: 0.75rem;
    line-height: 1.4;
    white-space: nowrap;
    user-select: none;
    cursor: default;
  }

  // A quoted selection: the same atomic shape as a file mention, tinted as a
  // quote; its `data-quote-text` turns into a blockquote on send.
  :deep(.text-quote-chip) {
    display: inline-flex;
    align-items: center;
    gap: 0.25rem;
    padding: 0 0.375rem;
    margin: 0 0.125rem;
    border-radius: 0.375rem;
    background-color: color-mix(in srgb, var(--color-theme-main) 7%, transparent);
    border-left: 2px solid var(--color-theme-main);
    color: var(--color-theme-main);
    font-size: 0.75rem;
    line-height: 1.4;
    max-width: 20rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    user-select: none;
    cursor: default;
  }

  // Send/stop buttons cluster: pinned bottom-right; the stop button (rendered only while
  // generating) sits left of the always-present send button via flex layout
  > .sendActions {
    position: absolute;
    right: 0.5rem;
    bottom: 0.5rem;
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  // Recording timer next to the mic button: tabular digits keep the mm:ss steady.
  > .sendActions > .voiceTimer {
    font-size: 0.75rem;
    color: #ef4444;
    font-variant-numeric: tabular-nums;
    user-select: none;
  }

  // The recording mic breathes instead of blinking — a stop control you can
  // still aim at, never a seizure risk.
  > .sendActions > .voice.voice-recording {
    animation: voice-pulse 1.2s ease-in-out infinite;
  }
}

@keyframes voice-pulse {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.5;
  }
}
</style>
