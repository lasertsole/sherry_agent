import 'fake-indexeddb/auto';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  SHORTCUT_BINDINGS,
  comboFromEvent,
  combosEqual,
  effectiveKeys,
  findConflict,
  formatCombo,
  installGlobalShortcutListener,
  matchesBinding,
  registerGlobalHandler,
  resetShortcutBinding,
  saveShortcutBinding,
  unregisterGlobalHandler
} from '../use-shortcuts';

const plain = { ctrl: false, shift: false, alt: false, meta: false };

/**
 * A keydown as the browser reports it.
 * @param k
 * @param over
 */
const key = (k: string, over: Partial<KeyboardEventInit> = {}): KeyboardEvent =>
  new KeyboardEvent('keydown', { key: k, bubbles: true, ...over });

describe('comboFromEvent', () => {
  it('normalizes the platform modifier and lowercases the key', () => {
    expect(comboFromEvent(key('K', { ctrlKey: true }))).toEqual({ ...plain, ctrl: true, key: 'k' });
  });

  it('folds Cmd into ctrl so one table serves both platforms', () => {
    expect(comboFromEvent(key('k', { metaKey: true }))).toEqual({ ...plain, ctrl: true, key: 'k' });
  });

  it('keeps shift and alt separate', () => {
    expect(comboFromEvent(key('F', { ctrlKey: true, shiftKey: true, altKey: true }))).toEqual({
      ...plain,
      ctrl: true,
      shift: true,
      alt: true,
      key: 'f'
    });
  });
});

describe('combosEqual', () => {
  it('compares every field', () => {
    const base = { ...plain, key: 'enter' };
    expect(combosEqual(base, { ...base })).toBe(true);
    expect(combosEqual(base, { ...base, shift: true })).toBe(false);
    expect(combosEqual(base, { ...base, key: 'space' })).toBe(false);
  });
});

describe('formatCombo', () => {
  it('renders the word form on Windows/Linux', () => {
    expect(formatCombo({ ...plain, ctrl: true, shift: true, key: 'f' }, false)).toBe('Ctrl+Shift+F');
  });

  it('renders the glyph form on macOS', () => {
    expect(formatCombo({ ...plain, ctrl: true, shift: true, key: 'f' }, true)).toBe('⌘⇧F');
  });

  it('names the keys a browser reports as long strings', () => {
    expect(formatCombo({ ...plain, key: 'arrowup' }, false)).toBe('↑');
    expect(formatCombo({ ...plain, key: 'escape' }, false)).toBe('Esc');
    expect(formatCombo({ ...plain, key: 'enter' }, false)).toBe('Enter');
  });

  it('returns an empty string for no key', () => {
    expect(formatCombo({ ...plain, key: '' }, false)).toBe('');
  });
});

describe('effectiveKeys / matchesBinding', () => {
  it('falls back to the built-in default', () => {
    expect(effectiveKeys('command-palette')).toEqual({ ...plain, ctrl: true, key: 'k' });
  });

  it('returns null for an unknown id', () => {
    expect(effectiveKeys('nope')).toBeNull();
  });

  it('matches a synthesized event against the default chord', () => {
    expect(matchesBinding(key('k', { ctrlKey: true }), 'command-palette')).toBe(true);
    expect(matchesBinding(key('k'), 'command-palette')).toBe(false);
    expect(matchesBinding(key('f', { ctrlKey: true, shiftKey: true }), 'find-in-conversation')).toBe(true);
  });

  it('keeps the composer defaults identical to the pre-shortcut modifiers', () => {
    expect(effectiveKeys('send-message')).toEqual({ ...plain, key: 'enter' });
    expect(effectiveKeys('history-prev')).toEqual({ ...plain, key: 'arrowup' });
    expect(effectiveKeys('history-next')).toEqual({ ...plain, key: 'arrowdown' });
    expect(effectiveKeys('cancel-input')).toEqual({ ...plain, key: 'escape' });
  });

  it('does not confuse Shift+Enter with the send chord', () => {
    expect(matchesBinding(key('Enter', { shiftKey: true }), 'send-message')).toBe(false);
  });
});

describe('findConflict', () => {
  it('reports the binding that already owns a chord', () => {
    expect(findConflict({ ...plain, ctrl: true, key: 'k' })?.id).toBe('command-palette');
  });

  it('ignores the binding being rebound', () => {
    expect(findConflict({ ...plain, ctrl: true, key: 'k' }, 'command-palette')).toBeNull();
  });

  it('is null for an unused chord', () => {
    expect(findConflict({ ...plain, ctrl: true, alt: true, key: 'z' })).toBeNull();
  });
});

describe('global dispatch', () => {
  let uninstall: (() => void) | null = null;

  beforeEach(() => {
    uninstall = installGlobalShortcutListener();
  });

  afterEach(() => {
    uninstall?.();
    uninstall = null;
    for (const binding of SHORTCUT_BINDINGS) void resetShortcutBinding(binding.id);
    unregisterGlobalHandler('command-palette');
    unregisterGlobalHandler('toggle-sidebar');
    vi.restoreAllMocks();
  });

  it('routes a registered chord to its handler', async () => {
    const palette = vi.fn();
    registerGlobalHandler('command-palette', palette);

    document.dispatchEvent(key('k', { ctrlKey: true }));

    expect(palette).toHaveBeenCalledTimes(1);
  });

  it('ignores chords nobody registered and wrong modifiers', () => {
    const palette = vi.fn();
    registerGlobalHandler('command-palette', palette);

    document.dispatchEvent(key('k'));
    document.dispatchEvent(key('j', { ctrlKey: true }));

    expect(palette).not.toHaveBeenCalled();
  });

  it('follows a REBIND (the default stops firing, the new chord starts)', async () => {
    const palette = vi.fn();
    registerGlobalHandler('command-palette', palette);
    await saveShortcutBinding('command-palette', { ...plain, ctrl: true, shift: true, key: 'p' });

    document.dispatchEvent(key('k', { ctrlKey: true }));
    expect(palette).not.toHaveBeenCalled();

    document.dispatchEvent(key('p', { ctrlKey: true, shiftKey: true }));
    expect(palette).toHaveBeenCalledTimes(1);
  });

  it('stops firing after the override is reset', async () => {
    const palette = vi.fn();
    registerGlobalHandler('command-palette', palette);
    await saveShortcutBinding('command-palette', { ...plain, alt: true, key: 'x' });

    await resetShortcutBinding('command-palette');
    document.dispatchEvent(key('x', { altKey: true }));
    expect(palette).not.toHaveBeenCalled();

    document.dispatchEvent(key('k', { ctrlKey: true }));
    expect(palette).toHaveBeenCalledTimes(1);
  });

  it('only unregisters a handler its owner still holds', () => {
    const first = vi.fn();
    const second = vi.fn();
    registerGlobalHandler('command-palette', first);
    registerGlobalHandler('command-palette', second);

    unregisterGlobalHandler('command-palette', first); // stale page: ignored
    document.dispatchEvent(key('k', { ctrlKey: true }));
    expect(second).toHaveBeenCalledTimes(1);
    expect(first).not.toHaveBeenCalled();
  });
});
