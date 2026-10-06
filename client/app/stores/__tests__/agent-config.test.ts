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
  tools: [
    { name: 'read_file', group: 'files' },
    { name: 'write_file', group: 'files' },
    { name: 'terminal', group: 'terminal' }
  ],
  middlewares: [
    { name: 'HumanInTheLoop', required: true, gateable: false },
    { name: 'TaskIntentMiddleware', required: false, gateable: true }
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

  it('loads the catalogue once and groups the tools in catalogue order', async () => {
    const store = useAgentConfigStore();

    await store.loadCatalog();
    await store.loadCatalog();

    expect(bridge.fetchAgentCatalog).toHaveBeenCalledTimes(1);
    expect(store.toolGroups.map(group => group.group)).toEqual(['files', 'terminal']);
    expect(store.toolGroups[0]!.tools.map(tool => tool.name)).toEqual(['read_file', 'write_file']);
    expect(store.middlewares.gateable.map(entry => entry.name)).toEqual(['TaskIntentMiddleware']);
    expect(store.middlewares.locked.map(entry => entry.name)).toEqual(['HumanInTheLoop']);
  });

  it('expands an unset tool selection to every catalogue tool', async () => {
    const store = useAgentConfigStore();
    await store.loadCatalog();
    await store.hydrate('s1');

    expect(store.enabledTools('s1')).toEqual(['read_file', 'write_file', 'terminal']);
    expect(store.configOf('s1')).toEqual({});
  });

  it('reports a configured subset verbatim and mirrors a save', async () => {
    bridge.fetchAgentConfig.mockResolvedValue({
      config: { tools: ['terminal'], middlewares_disabled: ['TaskIntentMiddleware'] },
      pending: true
    });
    const store = useAgentConfigStore();
    await store.loadCatalog();
    await store.hydrate('s1');

    expect(store.enabledTools('s1')).toEqual(['terminal']);
    expect(store.isPending('s1')).toBe(true);

    bridge.setAgentConfig.mockResolvedValue({ config: { tools: ['read_file'] }, pending: false });
    await store.save('s1', { tools: ['read_file'] });

    expect(bridge.setAgentConfig).toHaveBeenCalledWith('s1', { tools: ['read_file'] });
    expect(store.enabledTools('s1')).toEqual(['read_file']);
    expect(store.isPending('s1')).toBe(false);
  });

  it('unions the catalogue’s required tools into the effective set', async () => {
    bridge.fetchAgentCatalog.mockResolvedValue({
      ...CATALOG,
      tools: [
        { name: 'read_file', group: 'files', required: true },
        { name: 'write_file', group: 'files' },
        { name: 'terminal', group: 'terminal' }
      ]
    });
    // A legacy payload that predates the required flag: the stored subset omits
    // `read_file`, the effective set still contains it (catalogue order).
    bridge.fetchAgentConfig.mockResolvedValue({ config: { tools: ['terminal'] }, pending: false });
    const store = useAgentConfigStore();
    await store.loadCatalog();
    await store.hydrate('s1');

    expect(store.enabledTools('s1')).toEqual(['read_file', 'terminal']);
    // An unset selection stays "everything on", required flag or not.
    expect(store.enabledTools('s2')).toEqual(['read_file', 'write_file', 'terminal']);
  });
});
