import { describe, it, expect } from 'vitest';
import { watch } from 'vue';
import { useDialogManager } from '../dialog-manager';

describe('useDialogManager', () => {
  it('initializes every registered id closed', () => {
    const dialogs = useDialogManager(['a', 'b'] as const);

    expect(dialogs.visible).toEqual({ a: false, b: false });
    expect(dialogs.isOpen('a')).toBe(false);
    expect(dialogs.isOpen('b')).toBe(false);
  });

  it('opens and closes only the targeted dialog', () => {
    const dialogs = useDialogManager(['a', 'b'] as const);

    dialogs.open('a');
    expect(dialogs.isOpen('a')).toBe(true);
    expect(dialogs.isOpen('b')).toBe(false);

    dialogs.close('a');
    expect(dialogs.isOpen('a')).toBe(false);
  });

  it('toggles a dialog back and forth', () => {
    const dialogs = useDialogManager(['only'] as const);

    dialogs.toggle('only');
    expect(dialogs.isOpen('only')).toBe(true);

    dialogs.toggle('only');
    expect(dialogs.isOpen('only')).toBe(false);
  });

  it('keeps the visibility flags reactive for template bindings', () => {
    const dialogs = useDialogManager(['a', 'b'] as const);
    const seen: boolean[] = [];
    const stop = watch(
      () => dialogs.visible.a,
      value => seen.push(value),
      { immediate: true, flush: 'sync' }
    );

    dialogs.open('a');
    dialogs.close('a');
    stop();

    expect(seen).toEqual([false, true, false]);
  });

  it('accepts a v-model write on the exposed flag (close path)', () => {
    const dialogs = useDialogManager(['a'] as const);

    dialogs.open('a');
    dialogs.visible.a = false; // what v-model does on the dialog's own update:modelValue

    expect(dialogs.isOpen('a')).toBe(false);
  });

  it('returns an independent manager per call', () => {
    const first = useDialogManager(['a'] as const);
    const second = useDialogManager(['a'] as const);

    first.open('a');
    expect(second.isOpen('a')).toBe(false);
    expect(first).not.toBe(second);
  });
});
