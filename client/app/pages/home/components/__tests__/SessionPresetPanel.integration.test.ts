/**
 * The session preset VIEW (a right-sidebar tab under 当前会话): it names the
 * preset the active session was created with and renders that preset's content
 * in the same four tabs the 预设 panel edits — strictly view-only, no other
 * preset selectable or previewable.
 */
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { locale as i18nLocale } from 'vue-i18n';
import { reactive } from 'vue';
import SessionPresetPanel from '@/pages/home/components/SessionPresetPanel.vue';
import type { PersonaPreset } from '@/composables/db';

const bridge = vi.hoisted(() => ({
  readSystemPrompt: vi.fn(),
  readSystemPromptTemplate: vi.fn(),
  writeSystemPrompt: vi.fn()
}));
vi.mock('~/composables/bridge', () => bridge);

/** Route double: the panel resolves the active session from it. */
const routeState = vi.hoisted(() => ({ sid: 'sid-1' }));
vi.mock('vue-router', () => ({
  useRoute: () => ({ params: { sid: routeState.sid }, path: `/home/${routeState.sid}`, query: {} })
}));

const db = vi.hoisted(() => {
  const presets: PersonaPreset[] = [];
  return {
    presets,
    readCachedSessionPreset: vi.fn(),
    DEFAULT_CACHED_CHARACTER: {
      aiName: '橘雪莉',
      aiAvatar: '/avatar/assistant.jpg',
      userName: '远野汉娜',
      userAvatar: '/avatar/user.jpg'
    },
    GLOBAL_SESSION_KEY: '__global__',
    cacheCharacter: vi.fn(async () => undefined),
    listPersonaPresets: vi.fn(async () => [...presets])
  };
});
vi.mock('@/composables/db', () => db);

const TEMPLATE = {
  'AGENTS.md': '# AGENTS.md\nrules',
  'SOUL.md': '# SOUL.md\nsoul',
  'USER.md': '# USER.md\nuser'
};

/** Functional doubles for the two stores the agent tabs read. */
const agentStoreState = vi.hoisted(() => ({ current: null as unknown }));
vi.stubGlobal('useAgentConfigStore', () => agentStoreState.current);
const profilesState = vi.hoisted(() => ({ current: null as unknown }));
vi.stubGlobal('useLlmProfilesStore', () => profilesState.current);

/**
 * Build the agent-config store double: a real catalogue, a session config and a
 * save spy that records what the tab wrote.
 * @param config The session's stored config ({} = every default).
 * @param pending Whether the backend reported a parked choice.
 */
function makeAgentStore(config: Record<string, unknown> = {}, pending = false) {
  const saved: Array<Record<string, unknown>> = [];
  return {
    saved,
    store: reactive({
      catalog: {
        tools: [
          { name: 'read_file', group: 'files' },
          { name: 'terminal', group: 'terminal' }
        ],
        middlewares: [
          { name: 'HumanInTheLoop', required: true, gateable: false },
          { name: 'TaskIntentMiddleware', required: false, gateable: true }
        ],
        subagent_roles: [{ role: 'researcher', model_tier: 'auxiliary', description: 'r' }]
      },
      catalogLoaded: true,
      toolGroups: [
        {
          group: 'files',
          tools: [{ name: 'read_file', group: 'files', description: 'Read a file with pagination.' }]
        },
        {
          group: 'terminal',
          tools: [{ name: 'terminal', group: 'terminal', description: 'Run shell commands.' }]
        }
      ],
      middlewares: {
        gateable: [{ name: 'TaskIntentMiddleware', required: false, gateable: true }],
        locked: [{ name: 'HumanInTheLoop', required: true, gateable: false }]
      },
      subagentRoles: [{ role: 'researcher', model_tier: 'auxiliary', description: 'r' }],
      loadCatalog: vi.fn(async () => undefined),
      hydrate: vi.fn(async () => undefined),
      configOf: () => config as never,
      enabledTools: () => (Array.isArray(config.tools) ? config.tools : ['read_file', 'terminal']) as never,
      disabledMiddlewares: () => (config.middlewares_disabled ?? []) as never,
      isPending: () => pending,
      save: vi.fn(async (_sid: string, next: Record<string, unknown>) => {
        saved.push(next);
      })
    })
  };
}

/**
 * Build the llm-profiles store double with one MAIN_LLM profile.
 */
function makeProfilesStore() {
  const profile = {
    id: 'p1',
    label: '测试档案',
    params: { MAIN_LLM_NAME: 'glm-4.6', MAIN_LLM_PROVIDER: 'zhipu', MAIN_LLM_API_KEY: 'k' }
  };
  return reactive({
    listFor: () => [profile],
    byId: (_group: string, id: string | null) => (id === 'p1' ? profile : undefined),
    toSessionProfile: (p: typeof profile) => ({
      id: p.id,
      label: p.label,
      provider: 'zhipu',
      model: 'glm-4.6',
      api_key: 'k'
    })
  });
}

const stubs = {
  Divider: { name: 'Divider', template: '<div class="dv" />' },
  ProgressSpinner: { name: 'ProgressSpinner', template: '<div class="spin" />' },
  TabView: { name: 'TabView', props: ['activeIndex'], template: '<div class="tv"><slot /></div>' },
  TabPanel: { name: 'TabPanel', props: ['value', 'header'], template: '<div class="tp"><slot /></div>' },
  Checkbox: {
    name: 'Checkbox',
    props: ['modelValue', 'disabled'],
    template: `<input class="cb" type="checkbox" :checked="modelValue" :disabled="disabled" />`
  },
  ToggleSwitch: {
    name: 'ToggleSwitch',
    props: ['modelValue', 'disabled'],
    template: `<input class="ts" type="checkbox" :checked="modelValue" :disabled="disabled" />`
  },
  Select: {
    name: 'Select',
    props: ['modelValue', 'options', 'optionLabel', 'optionValue'],
    emits: ['update:modelValue'],
    template: `<select class="sel" :value="modelValue" @change="$emit('update:modelValue', $event.target.value)"><option v-for="o in options" :key="o.value" :value="o.value">{{ o.label }}</option></select>`
  }
};

/**
 * Mount the panel for the given route session id.
 * @param sessionId Session the route points at.
 * @returns The mounted wrapper.
 */
async function mountPanel(sessionId = 'sid-1') {
  routeState.sid = sessionId;
  const wrapper = mount(SessionPresetPanel, { global: { stubs } });
  await flushPromises();
  return wrapper;
}

describe('SessionPresetButton', () => {
  afterEach(() => {
    i18nLocale.value = 'zh';
  });

  beforeEach(() => {
    db.presets.length = 0;
    vi.clearAllMocks();
    bridge.readSystemPromptTemplate.mockResolvedValue(TEMPLATE);
    agentStoreState.current = makeAgentStore().store;
    profilesState.current = makeProfilesStore();
  });

  it('names the bound preset and previews it in the four content tabs', async () => {
    db.readCachedSessionPreset.mockResolvedValue({ session_id: 'sid-1', preset_id: 'coding', preset_name: '编程助手' });
    const wrapper = await mountPanel();

    const panel = wrapper.get('[data-test="session-preset-panel"]');
    expect(panel.get('[data-test="session-preset-bound"]').text()).toBe('编程助手');
    // Preview: the operating rules, and 空 for the coding preset's blank soul / user profile.
    expect(panel.get('[data-test="session-preset-content-AGENTS.md"]').text()).toBe(TEMPLATE['AGENTS.md']);
    expect(panel.get('[data-test="session-preset-content-SOUL.md"]').text()).toBe('（空）');
    expect(panel.get('[data-test="session-preset-content-USER.md"]').text()).toBe('（空）');
  });

  it('hints that the session has no preset instead of showing one', async () => {
    db.readCachedSessionPreset.mockResolvedValue(undefined);
    const wrapper = await mountPanel('sid-old');

    const panel = wrapper.get('[data-test="session-preset-panel"]');
    expect(panel.get('[data-test="session-preset-bound"]').text()).toBe('未选择');
    expect(panel.get('[data-test="session-preset-unavailable"]').text()).toBe('此会话创建时未选择预设');
    // Nothing to preview: no content tabs at all.
    expect(panel.find('[data-test="session-preset-content-AGENTS.md"]').exists()).toBe(false);
  });

  it('is read-only: no preset list, no other preset viewable, nothing written', async () => {
    db.presets.push({
      id: 7,
      name: '另一个预设',
      content: { 'AGENTS.md': 'OTHER', 'SOUL.md': 'S', 'USER.md': 'U' },
      createdAt: 0,
      updatedAt: 0
    });
    db.readCachedSessionPreset.mockResolvedValue({ session_id: 'sid-1', preset_id: 'coding', preset_name: '编程助手' });
    const wrapper = await mountPanel();

    const panel = wrapper.get('[data-test="session-preset-panel"]');
    // The session's OWN preset is all that is rendered: no entry list, and the
    // other presets' content never appears.
    expect(panel.findAll('[data-test^="session-preset-entry-"]')).toHaveLength(0);
    expect(panel.text()).not.toContain('另一个预设');
    expect(panel.get('[data-test="session-preset-content-AGENTS.md"]').text()).toBe(TEMPLATE['AGENTS.md']);
    expect(panel.get('[data-test="session-preset-content-AGENTS.md"]').text()).not.toBe('OTHER');
    // View-only: neither the persona files nor the global character were written.
    expect(bridge.writeSystemPrompt).not.toHaveBeenCalled();
    expect(db.cacheCharacter).not.toHaveBeenCalled();
  });

  it('keeps the bound name and says the preset is gone when a saved preset was deleted', async () => {
    db.readCachedSessionPreset.mockResolvedValue({
      session_id: 'sid-1',
      preset_id: 'user:999',
      preset_name: '已删除的预设'
    });
    const wrapper = await mountPanel();

    const panel = wrapper.get('[data-test="session-preset-panel"]');
    expect(panel.get('[data-test="session-preset-bound"]').text()).toBe('已删除的预设');
    expect(panel.get('[data-test="session-preset-unavailable"]').text()).toBe('该预设已被删除');
  });

  it('shows the session tools and middlewares READ-ONLY, in the preset panel’s shape', async () => {
    db.readCachedSessionPreset.mockResolvedValue({ session_id: 'sid-1', preset_id: 'coding', preset_name: '编程助手' });
    agentStoreState.current = makeAgentStore({
      tools: ['terminal'],
      middlewares_disabled: ['TaskIntentMiddleware']
    }).store;

    const wrapper = await mountPanel();

    const panel = wrapper.get('[data-test="session-preset-panel"]');
    // Tools: one disabled checkbox per catalogue tool, checked per the SESSION.
    const terminal = panel.get('[data-test="session-preset-tool-terminal"]');
    const readFile = panel.get('[data-test="session-preset-tool-read_file"]');
    expect(terminal.attributes('disabled')).toBeDefined();
    expect(readFile.attributes('disabled')).toBeDefined();
    expect((terminal.element as HTMLInputElement).checked).toBe(true);
    expect((readFile.element as HTMLInputElement).checked).toBe(false);
    // Hovering a row shows the backend's tool description.
    expect(readFile.element.closest('label')?.getAttribute('title')).toBe('Read a file with pagination.');
    // Middlewares: a disabled switch mirroring the disabled set + the locked list.
    const switchEl = panel.get('[data-test="session-preset-middleware-TaskIntentMiddleware"]');
    expect(switchEl.attributes('disabled')).toBeDefined();
    expect((switchEl.element as HTMLInputElement).checked).toBe(false);
    expect(panel.text()).toContain('HumanInTheLoop');
    // Neither write path was touched (read-only).
    expect((agentStoreState.current as { save: ReturnType<typeof vi.fn> }).save).not.toHaveBeenCalled();
  });

  it('writes a per-role model choice to the SESSION, merged and parked-ready', async () => {
    db.readCachedSessionPreset.mockResolvedValue({ session_id: 'sid-1', preset_id: 'coding', preset_name: '编程助手' });
    agentStoreState.current = makeAgentStore({ tools: ['terminal'] }).store;

    const store = agentStoreState.current as { save: ReturnType<typeof vi.fn> };
    const wrapper = await mountPanel();
    const picker = wrapper.get('[data-test="session-preset-role-model-researcher"]');
    await picker.setValue('p1');
    await flushPromises();

    expect(store.save).toHaveBeenCalledTimes(1);
    const [sid, payload] = store.save.mock.calls[0] as [string, Record<string, unknown>];
    expect(sid).toBe('sid-1');
    // The tool selection survives (the payload is merged, not replaced).
    expect(payload.tools).toEqual(['terminal']);
    expect(payload.subagent_models).toEqual({
      researcher: { id: 'p1', label: '测试档案', provider: 'zhipu', model: 'glm-4.6', api_key: 'k' }
    });
  });

  it('flags a parked model choice as 下一轮生效', async () => {
    db.readCachedSessionPreset.mockResolvedValue({ session_id: 'sid-1', preset_id: 'coding', preset_name: '编程助手' });
    agentStoreState.current = makeAgentStore({}, true).store;

    const wrapper = await mountPanel();

    expect(wrapper.find('[data-test="session-preset-models-pending"]').exists()).toBe(true);
    expect(wrapper.text()).toContain('下一轮生效');
  });

  it('re-reads the language template when the UI locale changes', async () => {
    // The i18n plugin settles the locale after first mount, so a viewer that only
    // watched the session would keep the pre-settle language (live bug: an
    // English template previewed inside a Chinese app).
    db.readCachedSessionPreset.mockResolvedValue({ session_id: 'sid-1', preset_id: 'coding', preset_name: '编程助手' });
    const wrapper = await mountPanel();
    expect(bridge.readSystemPromptTemplate).toHaveBeenCalledWith('zh');

    i18nLocale.value = 'en';
    await flushPromises();

    expect(bridge.readSystemPromptTemplate).toHaveBeenLastCalledWith('en');
    // And the panel's own labels follow the locale.
    expect(wrapper.get('[data-test="session-preset-panel"]').text()).toContain('Session preset');
  });
});
