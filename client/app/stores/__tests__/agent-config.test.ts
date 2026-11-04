import { describe, it, expect, vi, beforeEach } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';

const bridge = vi.hoisted(() => ({
  fetchAgentCatalog: vi.fn(),
  fetchAgentConfig: vi.fn(),
  setAgentConfig: vi.fn()
}));
vi.mock('~/composables/bridge/agent-config', () => bridge);

import { useAgentConfigStore } from '../agent-config';

const CATALOG = {
  skills: [
    { name: 'image_to_text', builtin: true, description: 'media', required: true },
    { name: 'alpha', builtin: true, description: 'a' },
    { name: 'beta', builtin: true, description: 'b' },
    { name: 'uploaded', builtin: false, description: 'c' }
  ],
  subagent_roles: [{ role: 'researcher', model_tier: 'auxiliary', description: 'r' }]
};

describe('agent-config store', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.clearAllMocks();
    bridge.fetchAgentCatalog.mockResolvedValue(CATALOG);
    bridge.fetchAgentConfig.mockResolvedValue({ config: {}, pending: false });
    bridge.setAgentConfig.mockResolvedValue({ config: {}, pending: false });
  });

  it('loads the catalogue once (roles + skills only)', async () => {
    const store = useAgentConfigStore();

    await store.loadCatalog();
    await store.loadCatalog();

    expect(bridge.fetchAgentCatalog).toHaveBeenCalledTimes(1);
    expect(store.subagentRoles.map(role => role.role)).toEqual(['researcher']);
    expect(store.catalog).not.toHaveProperty('tools');
    expect(store.catalog).not.toHaveProperty('middlewares');
  });

  it('mirrors a save and its parked state', async () => {
    bridge.fetchAgentConfig.mockResolvedValue({
      config: { skills: ['alpha'] },
      pending: true
    });
    const store = useAgentConfigStore();
    await store.loadCatalog();
    await store.hydrate('s1');

    expect(store.selectedSkills('s1')).toEqual(['image_to_text', 'alpha']);
    expect(store.isPending('s1')).toBe(true);

    bridge.setAgentConfig.mockResolvedValue({ config: { skills: [] }, pending: false });
    await store.save('s1', { skills: [] });

    expect(bridge.setAgentConfig).toHaveBeenCalledWith('s1', { skills: [] });
    expect(store.selectedSkills('s1')).toEqual(['image_to_text']);
    expect(store.isPending('s1')).toBe(false);
  });

  it('splits the skills into 内置 / 第三方 and reports the session selection', async () => {
    const store = useAgentConfigStore();
    await store.loadCatalog();
    await store.hydrate('s1');

    expect(store.skills.builtin.map(skill => skill.name)).toEqual(['image_to_text', 'alpha', 'beta']);
    expect(store.skills.thirdParty.map(skill => skill.name)).toEqual(['uploaded']);
    // No opinion = null (the caller expands it to every skill)…
    expect(store.selectedSkills('s1')).toBeNull();

    // …an explicit selection is filtered to catalogue order, and an EMPTY one
    // stays empty (it means "no skill in the index", not "all of them").
    bridge.fetchAgentConfig.mockResolvedValue({ config: { skills: ['uploaded', 'alpha'] }, pending: false });
    await store.hydrate('s2');
    // Catalogue order, with the required chain unioned back in.
    expect(store.selectedSkills('s2')).toEqual(['image_to_text', 'alpha', 'uploaded']);

    bridge.fetchAgentConfig.mockResolvedValue({ config: { skills: [] }, pending: false });
    await store.hydrate('s3');
    // The required chain survives an explicit "nothing else" selection.
    expect(store.selectedSkills('s3')).toEqual(['image_to_text']);
  });
});
