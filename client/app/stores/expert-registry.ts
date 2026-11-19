/**
 * Expert registry store (UI upgrade S2).
 *
 * The expert-registry backend API is not shipped yet (its parent plan was not
 * part of this change set), so the store defaults to a deterministic in-memory
 * mock exactly as the S2 plan's Kill criteria prescribes. Flip `useMock` off
 * once `/api/experts` exists; the CRUD shape stays identical.
 */
import { defineStore } from 'pinia';
import { computed, ref } from 'vue';

export interface ExpertConfig {
  name: string;
  description: string;
  persona_prompt: string;
  model: string;
  thinking: string;
  functional_role: string;
  skills: string[];
  mcp_servers: string[];
  step_timeout_seconds: number;
}

export interface ExpertWithUsage extends ExpertConfig {
  team_count: number;
}

const MOCK_SEED: ExpertWithUsage[] = [
  {
    name: 'frontend-expert',
    description: 'Frontend development specialist',
    persona_prompt: 'You are a frontend expert specializing in Vue and Nuxt.',
    model: 'claude-3-5-sonnet',
    thinking: 'medium',
    functional_role: 'executor',
    skills: ['code_wiki', 'text_to_image'],
    mcp_servers: [],
    step_timeout_seconds: 1800,
    team_count: 2
  },
  {
    name: 'reviewer-expert',
    description: 'Code review and quality gate specialist',
    persona_prompt: 'You are a rigorous code reviewer.',
    model: 'gpt-4',
    thinking: 'high',
    functional_role: 'reviewer',
    skills: ['code_wiki'],
    mcp_servers: [],
    step_timeout_seconds: 1200,
    team_count: 0
  }
];

const emptyConfig = (): ExpertConfig => ({
  name: '',
  description: '',
  persona_prompt: '',
  model: 'claude-3-5-sonnet',
  thinking: 'medium',
  functional_role: 'executor',
  skills: [],
  mcp_servers: [],
  step_timeout_seconds: 1800
});

export const useExpertRegistryStore = defineStore('expert-registry', () => {
  const experts = ref<ExpertWithUsage[]>([]);
  const loading = ref(false);
  const error = ref<string | null>(null);
  /** Mock mode is the shipped default until the backend lands. */
  const useMock = ref(true);

  const byName = computed(() => new Map(experts.value.map(expert => [expert.name, expert])));

  async function loadAll(): Promise<void> {
    loading.value = true;
    error.value = null;
    try {
      if (useMock.value) {
        experts.value = MOCK_SEED.map(item => ({ ...item, skills: [...item.skills] }));
        return;
      }
      const res = await $fetch<{ experts: ExpertWithUsage[] }>('/api/experts');
      experts.value = res.experts;
    } catch (err) {
      error.value = String(err);
      // Fail-open: an unavailable backend must not break the UI (S2 acceptance #11).
      experts.value = [];
    } finally {
      loading.value = false;
    }
  }

  async function create(config: ExpertConfig): Promise<void> {
    if (!useMock.value) {
      await $fetch('/api/experts', { method: 'POST', body: config });
      // Re-read the server list only in live mode: the mock's loadAll() would
      // re-seed from MOCK_SEED and erase the just-created entry.
      await loadAll();
    } else if (!byName.value.has(config.name)) {
      experts.value = [...experts.value, { ...config, team_count: 0 }];
    }
  }

  async function update(name: string, patch: Partial<ExpertConfig>): Promise<void> {
    if (!useMock.value) {
      await $fetch(`/api/experts/${encodeURIComponent(name)}`, { method: 'PUT', body: patch });
      await loadAll();
    } else {
      experts.value = experts.value.map(item => (item.name === name ? { ...item, ...patch } : item));
    }
  }

  async function remove(name: string): Promise<void> {
    if (!useMock.value) {
      await $fetch(`/api/experts/${encodeURIComponent(name)}`, { method: 'DELETE' });
      await loadAll();
    } else {
      experts.value = experts.value.filter(item => item.name !== name);
    }
  }

  /** Blank form model for the create flow. */
  function newConfig(): ExpertConfig {
    return emptyConfig();
  }

  return { experts, loading, error, useMock, byName, loadAll, create, update, remove, newConfig };
});
