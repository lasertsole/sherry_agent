/**
 * Keyboard shortcuts: one binding table, one runtime matcher, one global
 * dispatcher, persisted user overrides.
 *
 * Two scopes, deliberately different owners:
 *
 *  - `composer` bindings only mean something while the composer has focus
 *    (Enter sends, ↑/↓ walk the question history, Esc leaves browse mode), so
 *    the composer itself matches them (`inputBox.vue::onComposerKeydown`) —
 *    the handler needs the draft/history state that lives there.
 *  - `global` / `navigation` bindings are dispatched from ONE document
 *    listener, installed by `plugins/shortcuts.ts`; the module that owns the
 *    action registers a handler with `registerGlobalHandler(id, fn)`.
 *
 * Every match is a runtime comparison against the EFFECTIVE keys (user override
 * ?? built-in default), so rebinding works without re-registering anything and
 * the defaults reproduce the pre-existing behaviour exactly.
 *
 * `ctrl` means the platform's primary modifier: Ctrl on Windows/Linux, Command
 * on macOS. A physical Cmd+… press is normalized into `ctrl`, so one table
 * describes both platforms (the UI renders it with `formatCombo`, which picks
 * ⌘ on macOS).
 *
 * @module composables/use-shortcuts
 */
import { reactive } from 'vue';

/** One key combination. `key` is the lowercased `KeyboardEvent.key`. */
export interface KeyCombo {
  /** Platform primary modifier (Ctrl, or Command on macOS). */
  ctrl: boolean;
  shift: boolean;
  alt: boolean;
  /** Reserved: no binding uses it (the primary modifier is folded into `ctrl`). */
  meta: boolean;
  /** Lowercase `KeyboardEvent.key`: 'enter' / 'arrowup' / 'k' / ' ' … */
  key: string;
}

/** Where a binding is active. */
export type ShortcutScope = 'composer' | 'global';

/** Grouping for the settings list. */
export type ShortcutCategory = 'composer' | 'navigation' | 'global';

/** One registrable shortcut. */
export interface ShortcutBinding {
  /** Stable id: the override and the handler registry are keyed by it. */
  id: string;
  /** i18n key of the human label. */
  label: string;
  /** Built-in combination — matched exactly until the user rebinds. */
  defaultKeys: KeyCombo;
  category: ShortcutCategory;
  scope: ShortcutScope;
}

/**
 * A plain (modifier-free) combo, used as the base of the table below.
 * @param key Lowercase `KeyboardEvent.key`.
 */
const plain = (key: string): KeyCombo => ({ ctrl: false, shift: false, alt: false, meta: false, key });

/**
 * Ctrl/Cmd + one key.
 * @param key
 * @param shift
 */
const ctrlKey = (key: string, shift = false): KeyCombo => ({
  ctrl: true,
  shift,
  alt: false,
  meta: false,
  key
});

/**
 * The built-in table. The composer defaults MUST stay the pre-shortcut-system
 * modifiers (Enter / ArrowUp / ArrowDown / Escape) so a user who never opens
 * the settings keeps byte-identical behaviour.
 */
export const SHORTCUT_BINDINGS: readonly ShortcutBinding[] = [
  {
    id: 'send-message',
    label: 'shortcuts.sendMessage',
    defaultKeys: plain('enter'),
    category: 'composer',
    scope: 'composer'
  },
  {
    id: 'history-prev',
    label: 'shortcuts.historyPrev',
    defaultKeys: plain('arrowup'),
    category: 'composer',
    scope: 'composer'
  },
  {
    id: 'history-next',
    label: 'shortcuts.historyNext',
    defaultKeys: plain('arrowdown'),
    category: 'composer',
    scope: 'composer'
  },
  {
    id: 'cancel-input',
    label: 'shortcuts.cancelInput',
    defaultKeys: plain('escape'),
    category: 'composer',
    scope: 'composer'
  },
  {
    id: 'command-palette',
    label: 'shortcuts.commandPalette',
    defaultKeys: ctrlKey('k'),
    category: 'global',
    scope: 'global'
  },
  {
    id: 'find-in-conversation',
    label: 'shortcuts.findInConversation',
    defaultKeys: ctrlKey('f', true),
    category: 'global',
    scope: 'global'
  },
  {
    id: 'new-session',
    label: 'shortcuts.newSession',
    defaultKeys: ctrlKey('n'),
    category: 'navigation',
    scope: 'global'
  },
  {
    id: 'toggle-sidebar',
    label: 'shortcuts.toggleSidebar',
    defaultKeys: ctrlKey('b'),
    category: 'navigation',
    scope: 'global'
  }
];

/** id → user override (module-level: the plugin, pages and panels share it). */
const overrides = reactive<Record<string, KeyCombo>>({});

/** id → the handler that performs the action (registered by its owner). */
const globalHandlers = new Map<string, () => void>();

/** Whether the Dexie overrides were already read in this process. */
let loaded = false;

/**
 * The combination an event represents (Cmd folded into `ctrl`).
 * @param event Keyboard event.
 */
export function comboFromEvent(event: KeyboardEvent): KeyCombo {
  return {
    ctrl: event.ctrlKey || event.metaKey,
    shift: event.shiftKey,
    alt: event.altKey,
    meta: false,
    key: event.key.toLowerCase()
  };
}

/**
 * Structural equality of two combinations.
 * @param a
 * @param b
 */
export function combosEqual(a: KeyCombo, b: KeyCombo): boolean {
  return a.ctrl === b.ctrl && a.shift === b.shift && a.alt === b.alt && a.meta === b.meta && a.key === b.key;
}

/** Display names for the keys a browser reports as long strings. */
const KEY_LABELS: Record<string, string> = {
  arrowup: '↑',
  arrowdown: '↓',
  arrowleft: '←',
  arrowright: '→',
  escape: 'Esc',
  enter: 'Enter',
  backspace: 'Backspace',
  delete: 'Delete',
  tab: 'Tab',
  ' ': 'Space'
};

/**
 * Render a combination for a keyboard badge ("Ctrl+Shift+F", "⌘⇧F" on macOS).
 * @param keys The combination.
 * @param mac Whether to render the macOS modifier glyphs (defaults to detection).
 */
export function formatCombo(keys: KeyCombo, mac = isMacPlatform()): string {
  if (!keys.key) return '';
  const parts: string[] = [];
  if (keys.ctrl) parts.push(mac ? '⌘' : 'Ctrl');
  if (keys.shift) parts.push(mac ? '⇧' : 'Shift');
  if (keys.alt) parts.push(mac ? '⌥' : 'Alt');
  const label = KEY_LABELS[keys.key] ?? (keys.key.length === 1 ? keys.key.toUpperCase() : keys.key);
  // The macOS glyphs read as one chord ("⌘⇧F"); the word form stays separator-joined.
  return mac ? `${parts.join('')}${label}` : [...parts, label].join('+');
}

/**
 * Whether this browser runs on macOS (drives the ⌘ glyphs).
 */
function isMacPlatform(): boolean {
  if (typeof navigator === 'undefined') return false;
  return /mac|iphone|ipad/i.test(navigator.userAgent);
}

/**
 * The combination in force for one id (user override ?? built-in default).
 * @param id Binding id.
 */
export function effectiveKeys(id: string): KeyCombo | null {
  const binding = SHORTCUT_BINDINGS.find(candidate => candidate.id === id);
  if (!binding) return null;
  return overrides[id] ?? binding.defaultKeys;
}

/**
 * Match a keyboard event against one binding's effective keys.
 * @param event Keyboard event.
 * @param id Binding id.
 */
export function matchesBinding(event: KeyboardEvent, id: string): boolean {
  const keys = effectiveKeys(id);
  return keys !== null && combosEqual(comboFromEvent(event), keys);
}

/**
 * The binding that already owns a combination, if any (the settings panel's
 * conflict warning). Every binding is checked, across scopes: a composer
 * binding on Ctrl+K would shadow the palette while typing.
 * @param keys The candidate combination.
 * @param excludeId The binding being rebound (it cannot conflict with itself).
 */
export function findConflict(keys: KeyCombo, excludeId?: string): ShortcutBinding | null {
  return (
    SHORTCUT_BINDINGS.find(binding => {
      if (binding.id === excludeId) return false;
      const current = effectiveKeys(binding.id);
      return current !== null && combosEqual(keys, current);
    }) ?? null
  );
}

/**
 * Read the persisted overrides once (idempotent; failure leaves the defaults).
 */
export async function loadShortcutBindings(): Promise<void> {
  if (loaded) return;
  loaded = true;
  try {
    const rows = await readShortcutRows();
    for (const row of rows) {
      try {
        const keys = JSON.parse(row.keys) as KeyCombo;
        if (keys && typeof keys.key === 'string') overrides[row.id] = keys;
      } catch {
        // A malformed row is ignored: the built-in default keeps working.
      }
    }
  } catch {
    // An unreadable store leaves every binding on its default.
  }
}

/**
 * Persist one override and apply it immediately.
 * @param id Binding id.
 * @param keys The new combination.
 */
export async function saveShortcutBinding(id: string, keys: KeyCombo): Promise<void> {
  overrides[id] = keys;
  try {
    await writeShortcutRow({ id, keys: JSON.stringify(keys) });
  } catch {
    // The in-memory override still applies for this session.
  }
}

/**
 * Drop one override (back to the built-in default).
 * @param id Binding id.
 */
export async function resetShortcutBinding(id: string): Promise<void> {
  delete overrides[id];
  try {
    await deleteShortcutRow(id);
  } catch {
    // Nothing to do: the default is already in force.
  }
}

/**
 * Register the action for one GLOBAL binding id.
 * @param id Binding id.
 * @param handler What to do when the chord is pressed.
 */
export function registerGlobalHandler(id: string, handler: () => void): void {
  globalHandlers.set(id, handler);
}

/**
 * Remove a global handler — only when this caller still owns it (a KeepAlive
 * page deactivating must not unhook the page that replaced it).
 * @param id Binding id.
 * @param handler The handler being withdrawn (omit to force-remove).
 */
export function unregisterGlobalHandler(id: string, handler?: () => void): void {
  if (handler && globalHandlers.get(id) !== handler) return;
  globalHandlers.delete(id);
}

/**
 * Dispatch one keydown to a registered global binding.
 * @param event The document keydown.
 */
function dispatchGlobal(event: KeyboardEvent): void {
  const combo = comboFromEvent(event);
  for (const binding of SHORTCUT_BINDINGS) {
    if (binding.scope !== 'global') continue;
    const keys = effectiveKeys(binding.id);
    if (!keys || !combosEqual(combo, keys)) continue;
    const handler = globalHandlers.get(binding.id);
    if (!handler) continue;
    event.preventDefault();
    handler();
    return;
  }
}

/**
 * Install the one document listener that drives global bindings.
 * @returns An uninstall function (the plugin's HMR teardown keeps one listener).
 */
export function installGlobalShortcutListener(): () => void {
  if (typeof document === 'undefined') return () => {};
  document.addEventListener('keydown', dispatchGlobal);
  return () => document.removeEventListener('keydown', dispatchGlobal);
}

/**
 * The controller surface shared by the composer, the pages and the settings
 * panel (module-level state: one table, one override map).
 */
export function useShortcuts() {
  return {
    bindings: SHORTCUT_BINDINGS,
    overrides,
    effectiveKeys,
    matchesBinding,
    findConflict,
    formatCombo,
    load: loadShortcutBindings,
    save: saveShortcutBinding,
    reset: resetShortcutBinding,
    registerGlobalHandler,
    unregisterGlobalHandler
  };
}
