/**
 * The session preset VIEW (a right-sidebar tab under This session): it names the
 * preset the active session was created with and renders that preset's content
 * in the same four tabs the preset panel edits — strictly view-only, no other
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
        subagent_roles: [{ role: 'researcher', model_tier: 'auxiliary', description: 'r' }],
        skills: [
          { name: 'image_to_text', builtin: true, description: 'Media in.', required: true },
          { name: 'alpha', builtin: true, description: 'Shipped skill.' },
          { name: 'uploaded', builtin: false, description: 'Uploaded skill.' }
        ]
      },
      catalogLoaded: true,
      subagentRoles: [{ role: 'researcher', model_tier: 'auxiliary', description: 'r' }],
      skills: {
        builtin: [
          { name: 'image_to_text', builtin: true, description: 'Media in.', required: true },
          { name: 'alpha', builtin: true, description: 'Shipped skill.' }
        ],
        thirdParty: [{ name: 'uploaded', builtin: false, description: 'Uploaded skill.' }]
      },
      loadCatalog: vi.fn(async () => undefined),
      hydrate: vi.fn(async () => undefined),
      configOf: () => config as never,
      selectedSkills: () => (Array.isArray(config.skills) ? config.skills : null) as never,
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
    }),
    toSubagentProfile: (p: typeof profile) => ({
      id: p.id,
      label: p.label,
      provider: 'zhipu',
      model: 'glm-4.6',
      api_key: 'k',
      max_output_tokens: '8192'
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

  it('names the bound preset and previews its persona files', async () => {
    db.readCachedSessionPreset.mockResolvedValue({ session_id: 'sid-1', preset_id: 'sherry', preset_name: '橘雪莉' });
    const wrapper = await mountPanel();

    const panel = wrapper.get('[data-test="session-preset-panel"]');
    expect(panel.get('[data-test="session-preset-bound"]').text()).toBe('橘雪莉');
    // Preview: the full preset's template content (the built-in resolves).
    // AGENTS.md is not previewed anymore; the two persona files are.
    expect(panel.find('[data-test="session-preset-content-AGENTS.md"]').exists()).toBe(false);
    expect(panel.find('[data-test="session-preset-content-SOUL.md"]').exists()).toBe(true);
    expect(panel.get('[data-test="session-preset-content-SOUL.md"]').text()).toBe(TEMPLATE['SOUL.md']);
    // The built-in ships no USER profile → the empty-state label.
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
    db.readCachedSessionPreset.mockResolvedValue({ session_id: 'sid-1', preset_id: 'sherry', preset_name: '橘雪莉' });
    const wrapper = await mountPanel();

    const panel = wrapper.get('[data-test="session-preset-panel"]');
    // The session's OWN preset is all that is rendered: no entry list, and the
    // other presets' content never appears.
    expect(panel.findAll('[data-test^="session-preset-entry-"]')).toHaveLength(0);
    expect(panel.text()).not.toContain('另一个预设');
    expect(panel.get('[data-test="session-preset-content-SOUL.md"]').text()).toBe(TEMPLATE['SOUL.md']);
    expect(panel.get('[data-test="session-preset-content-SOUL.md"]').text()).not.toBe('S');
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

  it('shows the session skills READ-ONLY with the two sub-tabs', async () => {
    db.readCachedSessionPreset.mockResolvedValue({ session_id: 'sid-1', preset_id: 'sherry', preset_name: '橘雪莉' });
    agentStoreState.current = makeAgentStore({ skills: ['alpha'] }).store;

    const wrapper = await mountPanel();
    const panel = wrapper.get('[data-test="session-preset-panel"]');

    // The session's own selection: alpha checked (and disabled), the other off.
    const alpha = panel.get('[data-test="session-preset-skill-alpha"]');
    expect(alpha.attributes('disabled')).toBeDefined();
    expect((alpha.element as HTMLInputElement).checked).toBe(true);
    expect(panel.text()).toContain('已选 1/3');
    // The third-party sub-tab carries its own list (and its own state).
    await panel.get('[data-test="session-preset-skills-scope-thirdparty"]').trigger('click');
    await flushPromises();
    const uploaded = panel.get('[data-test="session-preset-skill-uploaded"]');
    expect(uploaded.attributes('disabled')).toBeDefined();
    expect((uploaded.element as HTMLInputElement).checked).toBe(false);
    expect(panel.find('[data-test="session-preset-skill-alpha"]').exists()).toBe(false);
    // Nothing was written (read-only).
    expect((agentStoreState.current as { save: ReturnType<typeof vi.fn> }).save).not.toHaveBeenCalled();
  });

  it('leads with the 代理模型 tab and mirrors 主代理 read-only', async () => {
    db.readCachedSessionPreset.mockResolvedValue({ session_id: 'sid-1', preset_id: 'sherry', preset_name: '橘雪莉' });
    agentStoreState.current = makeAgentStore({}).store;

    const wrapper = await mountPanel();
    const panel = wrapper.get('[data-test="session-preset-panel"]');

    // Agent models leads the whole strip (the session's own model pair is what the
    // viewer is opened for), ahead of the persona tabs too.
    const headers = panel.findAllComponents({ name: 'TabPanel' }).map(c => c.props('header'));
    expect(headers).toEqual(['代理模型', '角色配置', '人格灵魂', '用户信息', '技能', '高级']);

    // Main agent is the SAME control as the toolbar's model switch: a picker bound to
    // the session-model store (the stub reports "env" with no model configured).
    expect(panel.get('[data-test="session-preset-models-scope-main"]').text()).toBe('主代理');
    expect(panel.text()).toContain('跟随模型配置');
    const mainPicker = panel.get('[data-test="session-preset-main-model"]');
    expect((mainPicker.element as HTMLSelectElement).value).toBe('env');
    // Subagent keeps the editable role pickers.
    await panel.get('[data-test="session-preset-models-scope-subagent"]').trigger('click');
    await flushPromises();
    expect(panel.find('[data-test="session-preset-role-model-researcher"]').exists()).toBe(true);
  });

  it('switches the session main model from the 主代理 picker, through the same store', async () => {
    db.readCachedSessionPreset.mockResolvedValue({ session_id: 'sid-1', preset_id: 'sherry', preset_name: '橘雪莉' });
    const selectCalls: Array<[string, unknown]> = [];
    const profiles = makeProfilesStore();
    vi.stubGlobal('useLlmProfilesStore', () => profiles);
    vi.stubGlobal('useSessionModelStore', () =>
      reactive({
        currentId: () => 'env',
        envModel: { provider: 'zhipu', model: 'glm-4.6' },
        isPending: () => false,
        hydrate: async () => {},
        select: async (sid: string, profile: unknown) => {
          selectCalls.push([sid, profile]);
        }
      })
    );

    const wrapper = await mountPanel();
    const picker = wrapper.get('[data-test="session-preset-main-model"]');
    await picker.setValue('p1');
    await flushPromises();

    // The store's own write path (optimistic + rollback + parking) does the work,
    // so the toolbar switch and this row can never disagree.
    expect(selectCalls).toHaveLength(1);
    expect(selectCalls[0]![0]).toBe('sid-1');
    expect(selectCalls[0]![1]).toEqual({
      id: 'p1',
      label: '测试档案',
      provider: 'zhipu',
      model: 'glm-4.6',
      api_key: 'k'
    });

    // Back to "follow the environment config" writes null.
    await picker.setValue('env');
    await flushPromises();
    expect(selectCalls[1]).toEqual(['sid-1', null]);
  });

  it('writes a per-role model choice to the SESSION, merged and parked-ready', async () => {
    db.readCachedSessionPreset.mockResolvedValue({ session_id: 'sid-1', preset_id: 'sherry', preset_name: '橘雪莉' });
    agentStoreState.current = makeAgentStore({}).store;

    const store = agentStoreState.current as { save: ReturnType<typeof vi.fn> };
    const wrapper = await mountPanel();
    // The Agent models tab leads with Main agent; the role pickers sit under Subagent.
    await wrapper.get('[data-test="session-preset-models-scope-subagent"]').trigger('click');
    await flushPromises();
    const picker = wrapper.get('[data-test="session-preset-role-model-researcher"]');
    await picker.setValue('p1');
    await flushPromises();

    expect(store.save).toHaveBeenCalledTimes(1);
    const [sid, payload] = store.save.mock.calls[0] as [string, Record<string, unknown>];
    expect(sid).toBe('sid-1');
    expect(payload.subagent_models).toEqual({
      researcher: {
        id: 'p1',
        label: '测试档案',
        provider: 'zhipu',
        model: 'glm-4.6',
        api_key: 'k',
        max_output_tokens: '8192'
      }
    });
  });

  it('edits the role profile output cap and keeps the selected profile', async () => {
    db.readCachedSessionPreset.mockResolvedValue({ session_id: 'sid-1', preset_id: 'sherry', preset_name: '橘雪莉' });
    agentStoreState.current = makeAgentStore({
      subagent_models: {
        researcher: {
          id: 'p1',
          label: '测试档案',
          provider: 'zhipu',
          model: 'glm-4.6',
          api_key: 'k',
          max_output_tokens: '8192'
        }
      }
    }).store;

    const store = agentStoreState.current as { save: ReturnType<typeof vi.fn> };
    const wrapper = await mountPanel();
    await wrapper.get('[data-test="session-preset-models-scope-subagent"]').trigger('click');
    await flushPromises();

    const input = wrapper.get('[data-test="session-preset-role-max-output-researcher"]');
    (input.element as HTMLInputElement).value = '16384';
    await input.trigger('change');
    await flushPromises();

    const [, payload] = store.save.mock.calls[0] as [string, Record<string, unknown>];
    expect(payload.subagent_models).toEqual({
      researcher: {
        id: 'p1',
        label: '测试档案',
        provider: 'zhipu',
        model: 'glm-4.6',
        api_key: 'k',
        max_output_tokens: '16384'
      }
    });
  });

  it('flags a parked model choice as 下一轮生效', async () => {
    db.readCachedSessionPreset.mockResolvedValue({ session_id: 'sid-1', preset_id: 'sherry', preset_name: '橘雪莉' });
    agentStoreState.current = makeAgentStore({}, true).store;

    const wrapper = await mountPanel();
    await wrapper.get('[data-test="session-preset-models-scope-subagent"]').trigger('click');
    await flushPromises();

    expect(wrapper.find('[data-test="session-preset-models-pending"]').exists()).toBe(true);
    expect(wrapper.text()).toContain('下一轮生效');
  });

  it('re-reads the language template when the UI locale changes', async () => {
    // The i18n plugin settles the locale after first mount, so a viewer that only
    // watched the session would keep the pre-settle language (live bug: an
    // English template previewed inside a Chinese app).
    db.readCachedSessionPreset.mockResolvedValue({ session_id: 'sid-1', preset_id: 'sherry', preset_name: '橘雪莉' });
    const wrapper = await mountPanel();
    expect(bridge.readSystemPromptTemplate).toHaveBeenCalledWith('zh');

    i18nLocale.value = 'en';
    await flushPromises();

    expect(bridge.readSystemPromptTemplate).toHaveBeenLastCalledWith('en');
    // And the panel's own labels follow the locale (the viewer's tab is 预设角色, "Presets & Role").
    expect(wrapper.get('[data-test="session-preset-panel"]').text()).toContain('Presets & Role');
  });
});
