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
      <!-- Discoverable entry for the hidden trigger characters: opens the same
          skill/command pickers as `$` and `/`, or the sidebar file tree. -->
      <Button
        v-if="!disabled"
        icon="pi pi-plus"
        class="insert-trigger"
        text
        rounded
        severity="secondary"
        :aria-label="t('chatInput.insertTrigger')"
        :title="t('chatInput.insertTrigger')"
        @click="toggleInsertMenu" />
      <Popover ref="insertMenuRef">
        <div class="insert-menu">
          <button
            type="button"
            class="insert-item"
            @click="openSkillsFromMenu">
            <span class="pi pi-bolt" />
            <span>{{ t('chatInput.skillShortcut') }}</span>
            <code>$</code>
          </button>
          <button
            type="button"
            class="insert-item"
            @click="openCommandsFromMenu">
            <span class="pi pi-slash" />
            <span>{{ t('chatInput.commandShortcut') }}</span>
            <code>/</code>
          </button>
          <button
            type="button"
            class="insert-item"
            @click="openFilesFromMenu">
            <span class="pi pi-folder" />
            <span>{{ t('chatInput.fileShortcut') }}</span>
            <code>@</code>
          </button>
        </div>
      </Popover>
      <!-- Voice input: mic → record → transcribe → insert at the caret (the
          button owns the recording state machine; the transcript comes back
          through `transcript` and is inserted by this composer). -->
      <VoiceRecordButton
        :disabled="disabled"
        @transcript="insertVoiceText" />
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

    <InputTriggerPanel
      v-if="triggerKind"
      ref="triggerPanelRef"
      :kind="triggerKind"
      :query="triggerQuery"
      :anchor="triggerAnchor"
      :session-id="sessionId"
      @select="onTriggerSelect"
      @close="closeTrigger" />
  </div>
</template>

<script lang="ts" setup>
import { computed, onMounted, watch, type ShallowRef } from 'vue';
import { isEmpty } from 'lodash-es';
import { useI18n } from 'vue-i18n';
// Type-only: the mention helpers themselves are auto-imported composable
// exports (the repo's `no-restricted-imports` rule keeps value imports out of
// components).
import type { FileMentionPayload } from '~/composables/use-file-mention';
import VoiceRecordButton from './VoiceRecordButton.vue';

const { t } = useI18n();

/** Whether an AI reply is currently being generated (controls the stop button; sending while streaming stays allowed — the backend enqueues the new message) */
const props = withDefaults(defineProps<Props>(), {
  sending: false,
  disabled: false,
  disabledText: '',
  sessionId: ''
});

interface Props {
  sending?: boolean;
  /** Whether input is forbidden (e.g. while a HITL request awaits approval, blocking manual input/sending) */
  disabled?: boolean;
  /** Placeholder text shown in the disabled state (e.g. "Waiting for approval…") */
  disabledText?: string;
  /** Current session, used for workspace-scoped custom commands. */
  sessionId?: string;
}

const emit = defineEmits<{
  /** Send a message; the payload is plain text content */
  (e: 'send', text: string): void;
  /** Stop the current AI reply generation */
  (e: 'stop'): void;
}>();

/** Input area DOM */
const inputDom: ShallowRef<HTMLElement | null> = useTemplateRef('inputDom');

/** Insert (+ button) popover, and the `$`/`/` candidate panel it can open. */
const insertMenuRef = useTemplateRef<{ toggle?: (event: Event) => void; hide?: () => void }>('insertMenuRef');
const triggerPanelRef = useTemplateRef<{
  move: (delta: number) => void;
  selectActive: () => void;
  close: () => void;
}>('triggerPanelRef');
const ui = useUiStore();

/** `$` / `/` picker state + insertion (the + menu opens the same two pickers). */
const { triggerKind, triggerQuery, triggerAnchor, openTrigger, closeTrigger, syncTriggerFromCaret, onTriggerSelect } =
  useComposerTrigger({ inputDom, isDisabled: () => props.disabled });

/** Workspace-file drop: highlight the composer and insert a mention chip. */
const { isDragOver, onDragOver, onDragLeave, onDrop } = useComposerFileDrop({
  insert: payload => insertFileMention(inputDom.value, payload)
});

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

/** ↑/↓/Esc question-history browser (the sent-questions cache). */
const { handleKeyArrowUp, handleKeyArrowDown, handleKeyEscape, recordQuestion, clearHistory } = useComposerHistory({
  draft,
  blocked: () => props.sending || props.disabled,
  writeDraft: writeDraftToInput
});

function toggleInsertMenu(event: Event): void {
  insertMenuRef.value?.toggle?.(event);
}

function openSkillsFromMenu(): void {
  insertMenuRef.value?.hide?.();
  openTrigger('skill');
}

function openCommandsFromMenu(): void {
  insertMenuRef.value?.hide?.();
  openTrigger('slash');
}

/** Files keep their existing left-sidebar tree; the + entry reveals it. */
function openFilesFromMenu(): void {
  insertMenuRef.value?.hide?.();
  ui.sidebarBody = 'files';
  ui.filesSectionOpen = true;
  ui.sidebarCollapsed = false;
}

/** Input callback: strip contenteditable markup, keeping only plain text for validation
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
  syncTriggerFromCaret(target);
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
  // The picker owns navigation while it is open; printable keys still reach the
  // composer so the query keeps updating through `inputFunc`.
  if (triggerKind.value && triggerPanelRef.value && !event.isComposing) {
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      triggerPanelRef.value.move(1);
      return;
    }
    if (event.key === 'ArrowUp') {
      event.preventDefault();
      triggerPanelRef.value.move(-1);
      return;
    }
    if (event.key === 'Enter') {
      event.preventDefault();
      triggerPanelRef.value.selectActive();
      return;
    }
    if (event.key === 'Escape') {
      event.preventDefault();
      closeTrigger();
      return;
    }
  }
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
});

watch(
  () => props.disabled,
  disabled => {
    if (disabled) closeTrigger();
  }
);

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
}

/* Insert-menu lives in a PrimeVue Popover teleported to <body>, so its styles
   must be global to escape this component's scoped attribute. */
:global(.insert-menu) {
  display: flex;
  min-width: 12rem;
  flex-direction: column;
  padding: 0.25rem;
}

:global(.insert-menu .insert-item) {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  border: 0;
  border-radius: 0.35rem;
  background: transparent;
  padding: 0.45rem 0.6rem;
  text-align: left;
  cursor: pointer;
}

:global(.insert-menu .insert-item:hover) {
  background: color-mix(in srgb, var(--p-primary-color, #0ea5e9) 14%, transparent);
}

:global(.insert-menu .insert-item code) {
  margin-left: auto;
  border-radius: 0.25rem;
  background: var(--p-surface-100, #f3f4f6);
  padding: 0 0.3rem;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 0.72rem;
}
</style>
