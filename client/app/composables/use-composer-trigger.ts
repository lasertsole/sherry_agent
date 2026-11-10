/**
 * Composer input-trigger wiring: the `$` skill picker and the `/` command
 * picker as the composer drives them.
 *
 * A trigger is detected at the caret after every composer input, anchored to
 * the composer's top edge, and dismissed on an outside pointerdown. Choosing a
 * row inserts an atomic chip at the caret (or runs a built-in command); the
 * detection/scoring/chip-building helpers stay in `use-input-trigger` /
 * `use-file-mention` — this slice owns the panel's open/close state and the
 * insertion.
 */
import { onBeforeUnmount, onMounted, ref } from 'vue';
import type { Ref } from 'vue';
import type { TriggerKind } from '~/composables/use-input-trigger';
import type { TriggerSelection } from '~/components/chat/InputTriggerPanel.vue';

/** The panel's anchor rectangle (viewport coordinates of the composer). */
export interface TriggerAnchor {
  left: number;
  top: number;
  width: number;
}

/** Collaborators of the composer trigger slice. */
export interface ComposerTriggerOptions {
  /** The contenteditable input element. */
  inputDom: Ref<HTMLElement | null>;
  /** Whether the composer forbids input. */
  isDisabled: () => boolean;
}

/**
 * Create the composer's input-trigger state.
 * @param options
 */
export function useComposerTrigger(options: ComposerTriggerOptions) {
  const triggerKind = ref<TriggerKind | null>(null);
  const triggerQuery = ref('');
  const triggerAnchor = ref<TriggerAnchor | null>(null);
  let triggerTokenLength = 0;

  /** Anchor the picker to the composer's top edge (upward expansion). */
  function currentAnchor(): TriggerAnchor | null {
    const rect = options.inputDom.value?.getBoundingClientRect();
    return rect ? { left: rect.left, top: rect.top, width: rect.width } : null;
  }

  /**
   * Open the `$`/`/` picker, optionally seeding the query (the + button path).
   * @param kind
   * @param query
   * @param tokenLength
   */
  function openTrigger(kind: TriggerKind, query = '', tokenLength = 0): void {
    triggerKind.value = kind;
    triggerQuery.value = query;
    triggerTokenLength = tokenLength;
    triggerAnchor.value = currentAnchor();
  }

  /** Dismiss the picker without touching the composer. */
  function closeTrigger(): void {
    triggerKind.value = null;
    triggerQuery.value = '';
    triggerAnchor.value = null;
    triggerTokenLength = 0;
  }

  /**
   * Refresh the trigger state from the caret after every composer input.
   * @param target
   */
  function syncTriggerFromCaret(target: HTMLElement): void {
    if (options.isDisabled() || isCaretInCode(target)) {
      closeTrigger();
      return;
    }
    const before = textBeforeCaret(target);
    const match = before === null ? null : detectTrigger(before, before.length);
    if (!match) {
      closeTrigger();
      return;
    }
    openTrigger(match.kind, match.query, match.tokenLength);
  }

  /**
   * Insert the selected skill/command (or run a built-in command).
   * @param selection
   */
  function onTriggerSelect(selection: TriggerSelection): void {
    const tokenLength = triggerTokenLength;
    if (tokenLength > 0) removeTriggerChars(tokenLength);

    if (selection.kind === 'skill') {
      insertChipAtCaret(options.inputDom.value, createSkillChip(selection.name));
      closeTrigger();
      return;
    }

    const command = selection.command;
    if (command?.kind === 'builtin') {
      options.inputDom.value?.focus();
      options.inputDom.value?.dispatchEvent(new InputEvent('input', { bubbles: true }));
      void command.entry?.handler?.();
    } else {
      const body = (command?.body ?? '').replaceAll('$ARGUMENTS', selection.args ?? '');
      insertChipAtCaret(options.inputDom.value, createCommandChip(selection.name, body || `/${selection.name}`));
    }
    closeTrigger();
  }

  /**
   * Outside pointerdown dismisses the picker (the composer itself keeps it).
   * @param event
   */
  function onDocumentPointerDown(event: PointerEvent): void {
    if (!triggerKind.value) return;
    const target = event.target as Node | null;
    if (
      target &&
      (options.inputDom.value?.contains(target) || (target as Element).closest?.('[data-test="input-trigger-panel"]'))
    ) {
      return;
    }
    closeTrigger();
  }

  onMounted(() => document.addEventListener('pointerdown', onDocumentPointerDown));
  onBeforeUnmount(() => document.removeEventListener('pointerdown', onDocumentPointerDown));

  return { triggerKind, triggerQuery, triggerAnchor, openTrigger, closeTrigger, syncTriggerFromCaret, onTriggerSelect };
}
