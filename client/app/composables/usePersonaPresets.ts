import { ref } from 'vue';
import {
  createPersonaPreset,
  deletePersonaPreset,
  listPersonaPresets,
  updatePersonaPreset,
  type PersonaPreset
} from '@/composables/db';

/**
 * Result of creating a persona preset through {@link usePersonaPresets}.
 *
 * `ok: false` + `reason: 'duplicate'` means the (trim + case-insensitive) name uniqueness
 * check failed; `reason: 'error'` covers every other (infrastructure) failure. Neither
 * case throws — the component layer decides how to surface the outcome (toasts etc.).
 */
export type PersonaPresetCreateResult = { ok: true; id: number } | { ok: false; reason: 'duplicate' | 'error' };

/**
 * Shared singleton for the AI persona preset list (module-level reactive state).
 *
 * Persona presets are a **global** (not per-session) feature: the "persona dialog" lists,
 * saves, overwrites and deletes them in Dexie's `personaPresets` table. So that "saving /
 * deleting takes effect immediately in every open view", the preset list must be a **true
 * module-level singleton** — `presets`/`loading` are declared at the module top level
 * (outside the function); every call to `usePersonaPresets()` returns a reference to the
 * **same** refs, not separate copies (same pattern as {@link useChatBackground}).
 *
 * - The first call automatically triggers one `refresh()` to fill the singleton state
 *   (fire-and-forget; later calls reuse the already-loaded list).
 * - `refresh()`: re-reads all presets from Dexie (createdAt ascending).
 * - `create(name, content)`: checks name uniqueness (trim + case-insensitive, delegated to
 *   `createPersonaPreset`), inserts, then refreshes the shared list. Never throws —
 *   duplicate names map to `{ ok: false, reason: 'duplicate' }`, any other failure to
 *   `{ ok: false, reason: 'error' }`.
 * - `update(id, content)`: overwrites one preset's content (never its name — "direct
 *   overwrite" semantics), then refreshes. Never throws — returns `false` on failure.
 * - `remove(id)`: deletes one preset by id, then refreshes. Never throws — returns
 *   `false` on failure.
 *
 * Note: none of the actions toast or touch the DOM — the component layer decides how to
 * surface success/duplicate/error outcomes.
 */

// ── Module-level shared state (the true singleton) ──
// All usePersonaPresets() callers share these same refs; changes via create/update/remove
// (each of which refreshes the list) take effect in every component immediately.
const presets = ref<PersonaPreset[]>([]);
const loading = ref(false);
/** Whether the one-time auto-refresh has been kicked off (runs once on first use) */
const presetsLoaded = ref(false);

/**
 * Module-level refresh: re-reads all presets from Dexie (createdAt ascending) into the
 * singleton state. Never throws — a local cache failure must not break the caller's flow.
 */
const refresh = async (): Promise<void> => {
  loading.value = true;
  try {
    presets.value = await listPersonaPresets();
  } catch (e) {
    console.error('[usePersonaPresets] Failed to load persona presets:', e);
  } finally {
    loading.value = false;
  }
};

/**
 * Module-level create: inserts a new preset after the name-uniqueness check (delegated
 * to `createPersonaPreset`, which throws on duplicates) and refreshes the shared list on
 * success. Never throws — see {@link PersonaPresetCreateResult} for the failure mapping.
 */
const create = async (name: string, content: Record<string, string>): Promise<PersonaPresetCreateResult> => {
  try {
    const id = await createPersonaPreset(name, content);
    await refresh();
    return { ok: true, id };
  } catch (e) {
    // createPersonaPreset throws an Error whose message contains "duplicate" when the
    // (trim + case-insensitive) name check fails; everything else is an infrastructure error.
    if (e instanceof Error && e.message.includes('duplicate')) {
      return { ok: false, reason: 'duplicate' };
    }
    console.error('[usePersonaPresets] Failed to create persona preset:', e);
    return { ok: false, reason: 'error' };
  }
};

/**
 * Module-level update: overwrites one preset's content (never its name) and refreshes
 * the shared list on success. Never throws — returns `false` on any failure.
 */
const update = async (id: number, content: Record<string, string>): Promise<boolean> => {
  try {
    await updatePersonaPreset(id, content);
    await refresh();
    return true;
  } catch (e) {
    console.error('[usePersonaPresets] Failed to update persona preset:', e);
    return false;
  }
};

/**
 * Module-level remove: deletes one preset by id and refreshes the shared list on
 * success. Never throws — returns `false` on any failure.
 */
const remove = async (id: number): Promise<boolean> => {
  try {
    await deletePersonaPreset(id);
    await refresh();
    return true;
  } catch (e) {
    console.error('[usePersonaPresets] Failed to delete persona preset:', e);
    return false;
  }
};

/**
 * Shared composable for the AI persona preset feature (module-level singleton, same
 * pattern as {@link useChatBackground}): every call returns references to the same
 * shared refs, plus the module-level actions.
 */
export function usePersonaPresets() {
  // Auto-refresh once on first use (fire-and-forget: refresh never rejects).
  if (!presetsLoaded.value) {
    presetsLoaded.value = true;
    void refresh();
  }

  return {
    presets,
    loading,
    refresh,
    create,
    update,
    remove
  };
}
