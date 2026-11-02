/**
 * Skills for the composer's `$` picker.
 *
 * The list is pulled once from `GET /skills`, filtered to active + main-visible
 * skills, and cached at module scope (the skill catalogue changes only after a
 * lifecycle action).
 *
 * @module composables/use-skills-list
 */

import { computed, ref, shallowRef } from 'vue';

import type { SkillInfo } from './bridge/skills';
// Stable specifier so tests can mock the bridge; the unimport injection would
// leave bare `listSkills` unmockable.
// eslint-disable-next-line @typescript-eslint/no-restricted-imports
import { listSkills } from './bridge/skills';

const cache = shallowRef<SkillInfo[] | null>(null);
const loading = ref(false);
let inflight: Promise<void> | null = null;

function usable(skill: SkillInfo): boolean {
  return skill.active !== false && skill.scope !== 'subagent_only';
}

/**
 * Load the catalogue once. Concurrent callers share one request; a failed
 * request leaves the cache empty so the next trigger retries.
 */
export async function ensureSkillsLoaded(): Promise<void> {
  if (cache.value !== null) return;
  if (inflight) return inflight;
  loading.value = true;
  inflight = (async () => {
    try {
      const response = await listSkills();
      cache.value = (response.skills ?? []).filter(usable).sort((a, b) => a.name.localeCompare(b.name));
    } catch {
      cache.value = null;
    } finally {
      loading.value = false;
      inflight = null;
    }
  })();
  return inflight;
}

/** Reactive skill list for the picker. */
export function useSkillsList() {
  return {
    skills: computed(() => cache.value ?? []),
    loading: computed(() => loading.value)
  };
}
