/**
 * The preset panel's two agent-config tabs (Agent models / Skills).
 *
 * Contract under test: the tabs render from the BACKEND catalogue (no
 * hardcoded names), the drafts map to the `agent` block (all-on / no-opinion
 * collapses to `{}`), and Save Preset stores the block while Apply writes it to the
 * open session through the store. The built-in tool set and the middleware chain
 * are NOT configurable, so those tabs are gone.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { reactive } from 'vue';
import PersonaPanel from '@/pages/home/components/PersonaPanel.vue';

const bridge = vi.hoisted(() => ({
  readSystemPrompt: vi.fn(),
  readSystemPromptTemplate: vi.fn(),
  writeSystemPrompt: vi.fn()
}));
vi.mock('~/composables/bridge', () => bridge);

const db = vi.hoisted(() => {
  const rows: Array<Record<string, unknown>> = [];
  return {
    rows,
    DEFAULT_CACHED_CHARACTER: {
      aiName: '橘雪莉',
      aiAvatar: '/avatar/assistant.jpg',
      userName: '远野汉娜',
      userAvatar: '/avatar/user.jpg'
    },
    GLOBAL_SESSION_KEY: '__global__',
    readCachedCharacter: vi.fn(async () => undefined),
    cacheCharacter: vi.fn(async () => undefined),
    listPersonaPresets: vi.fn(async () => [...rows]),
    createPersonaPreset: vi.fn(
      async (
        name: string,
        content: Record<string, string>,
        character?: Record<string, string>,
        agent?: Record<string, unknown>
      ) => {
        const id = rows.length + 1;
        rows.push({ id, name, content, character, agent, createdAt: 0, updatedAt: 0 });
        return id;
      }
    ),
    updatePersonaPreset: vi.fn(async () => undefined),
    deletePersonaPreset: vi.fn(async () => undefined)
  };
});
vi.mock('@/composables/db', () => db);

const agentStore = vi.hoisted(() => ({
  state: null as unknown
}));

vi.stubGlobal('useAgentConfigStore', () => agentStore.state);

/** One env-config MAIN_LLM profile, enough to exercise the role-model drafts. */
const llmProfilesStub = vi.hoisted(() => {
  const profile = {
    id: 'p1',
    label: '测试档案',
    params: { MAIN_LLM_NAME: 'glm-4.6', MAIN_LLM_PROVIDER: 'zhipu', MAIN_LLM_API_KEY: 'k' }
  };
  return {
    listFor: () => [profile],
    byId: (_group: string, id: string | null) => (id === 'p1' ? profile : undefined),
    toSessionProfile: () => ({
      id: 'p1',
      label: '测试档案',
      provider: 'zhipu',
      model: 'glm-4.6',
      api_key: 'k'
    }),
    toSubagentProfile: () => ({
      id: 'p1',
      label: '测试档案',
      provider: 'zhipu',
      model: 'glm-4.6',
      api_key: 'k',
      max_output_tokens: '8192'
    })
  };
});
vi.stubGlobal('useLlmProfilesStore', () => llmProfilesStub);

const AGENTS = '# AGENTS.md\nbody';
const SOUL = '# SOUL.md\nbody';
const USER = '# USER.md\nbody';

const stubs = {
  TabView: { name: 'TabView', props: ['activeIndex'], template: '<div class="tv"><slot /></div>' },
  TabPanel: { name: 'TabPanel', props: ['value', 'header'], template: '<div class="tp"><slot /></div>' },
  InputText: {
    name: 'InputText',
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: `<input class="it" :value="modelValue" @input="$emit('update:modelValue', $event.target.value)" />`
  },
  Textarea: {
    name: 'Textarea',
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: `<textarea class="ta" :value="modelValue" />`
  },
  Button: {
    name: 'Button',
    props: ['label', 'disabled', 'loading'],
    emits: ['click'],
    template: `<button class="btn" :disabled="disabled" @click="$emit('click')">{{ label }}</button>`
  },
  Checkbox: {
    name: 'Checkbox',
    props: ['modelValue', 'disabled'],
    emits: ['update:modelValue'],
    // `data-checked` mirrors the bound value for DOM-level assertions.
    template: `<button class="cb" :disabled="disabled" :data-checked="modelValue" @click="$emit('update:modelValue', !modelValue)">C</button>`
  },
  ToggleSwitch: {
    name: 'ToggleSwitch',
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: `<button class="ts" @click="$emit('update:modelValue', !modelValue)">T</button>`
  },
  Select: {
    name: 'Select',
    props: ['modelValue', 'options', 'optionLabel', 'optionValue'],
    emits: ['update:modelValue'],
    template: `<select class="sel" :value="modelValue" @change="$emit('update:modelValue', $event.target.value)"><option v-for="o in options" :key="o.value" :value="o.value">{{ o.label }}</option></select>`
  },
  FileUpload: { name: 'FileUpload', template: `<div class="fu"><slot name="filelabel" :files="[]" /></div>` },
  Divider: { name: 'Divider', template: '<div class="dv" />' },
  ProgressSpinner: { name: 'ProgressSpinner', template: '<div class="spin" />' },
  Dialog: {
    name: 'Dialog',
    props: ['visible', 'header'],
    emits: ['update:visible'],
    template: `<div class="dlg" v-if="visible"><slot /><slot name="footer" /></div>`
  },
  AvatarCropDialog: { name: 'AvatarCropDialog', template: '<div class="acd" />' }
};

/**
 * A functional store double: a real catalogue + the drafts a save receives.
 * @param overrides Per-test overrides (hydrated config).
 * @param overrides.config
 */
function makeAgentStore(overrides: { config?: Record<string, unknown> } = {}) {
  const saved: Array<Record<string, unknown>> = [];
  const store = reactive({
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
    configOf: () => (overrides.config ?? {}) as never,
    selectedSkills: () => (Array.isArray(overrides.config?.skills) ? overrides.config?.skills : null) as never,
    isPending: () => false,
    save: vi.fn(async (_sid: string, config: Record<string, unknown>) => {
      saved.push(config);
    })
  });
  return { store, saved };
}

async function mountPanel() {
  const panel = mount(PersonaPanel, { global: { stubs } });
  await flushPromises();
  return panel;
}

describe('PersonaPanel agent-config tabs', () => {
  beforeEach(() => {
    db.rows.length = 0;
    vi.clearAllMocks();
    bridge.readSystemPrompt.mockImplementation(async () => ({
      'AGENTS.md': AGENTS,
      'SOUL.md': SOUL,
      'USER.md': USER
    }));
    bridge.readSystemPromptTemplate.mockResolvedValue({
      'AGENTS.md': 'TPL-AGENTS',
      'SOUL.md': 'TPL-SOUL',
      'USER.md': 'TPL-USER'
    });
    bridge.writeSystemPrompt.mockResolvedValue(undefined);
    agentStore.state = makeAgentStore().store;
  });

  it('renders the agent + advanced tabs and drops the tool / middleware ones', async () => {
    const panel = await mountPanel();

    const headers = panel.findAllComponents({ name: 'TabPanel' }).map(c => c.props('header'));
    expect(headers.slice(-3)).toEqual(['代理模型', '技能', '高级']);
    expect(headers).not.toContain('工具');
    expect(headers).not.toContain('中间件');
    expect(panel.find('[data-test="agent-tools-tab"]').exists()).toBe(false);
    expect(panel.find('[data-test="agent-middlewares-tab"]').exists()).toBe(false);

    // The Skills tab carries its own two sub-tabs and the catalogue's rows.
    expect(panel.find('[data-test="agent-skills-tab"]').exists()).toBe(true);
    expect(panel.find('[data-test="agent-skills-scope-builtin"]').exists()).toBe(true);
    expect(panel.find('[data-test="agent-skills-scope-thirdparty"]').exists()).toBe(true);

    // The Agent models tab leads with Main agent and keeps the per-role pickers under Subagent.
    expect(panel.find('[data-test="agent-main-model"]').exists()).toBe(true);
    expect(panel.find('[data-test="agent-role-model-researcher"]').exists()).toBe(false);
    await panel.get('[data-test="agent-models-scope-subagent"]').trigger('click');
    await flushPromises();
    expect(panel.find('[data-test="agent-role-model-researcher"]').exists()).toBe(true);
    expect(panel.find('[data-test="agent-main-model"]').exists()).toBe(false);
  });

  it('stores a role profile output cap with the picked model', async () => {
    const panel = await mountPanel();
    await panel.get('[data-test="agent-models-scope-subagent"]').trigger('click');
    await flushPromises();

    await panel.get('[data-test="agent-role-model-researcher"]').setValue('p1');
    await flushPromises();
    const input = panel.get('[data-test="agent-role-max-output-researcher"]');
    expect((input.element as HTMLInputElement).value).toBe('8192');

    (input.element as HTMLInputElement).value = '16384';
    await input.trigger('change');

    const saveButton = panel.findAllComponents({ name: 'Button' }).find(b => b.props('label') === '保存预设');
    await saveButton!.trigger('click');
    await flushPromises();
    await panel.find('.dlg .it').setValue('角色输出预设');
    const confirm = panel.findAllComponents({ name: 'Button' }).find(b => b.props('label') === '保存');
    await confirm!.trigger('click');
    await flushPromises();

    expect(db.createPersonaPreset.mock.calls[0]![3]).toEqual({
      subagent_models: {
        researcher: {
          id: 'p1',
          label: '测试档案',
          provider: 'zhipu',
          model: 'glm-4.6',
          api_key: 'k',
          max_output_tokens: '16384'
        }
      }
    });
  });

  it('stores the skill draft in a saved preset and collapses "all on" to an empty block', async () => {
    const panel = await mountPanel();

    // Uncheck one optional skill (the required media skill has no switch).
    await panel.get('[data-test="agent-skill-alpha"]').trigger('click');
    await flushPromises();

    // Save Preset (no preset being edited → the name dialog opens) → confirm.
    const saveButton = panel.findAllComponents({ name: 'Button' }).find(b => b.props('label') === '保存预设');
    expect(saveButton).toBeTruthy();
    await saveButton!.trigger('click');
    await flushPromises();

    const nameInput = panel.find('.dlg .it');
    await nameInput.setValue('我的预设');
    const confirm = panel.findAllComponents({ name: 'Button' }).find(b => b.props('label') === '保存');
    expect(confirm, 'name dialog confirm').toBeTruthy();
    await confirm!.trigger('click');
    await flushPromises();

    const created = db.createPersonaPreset.mock.calls[0]!;
    expect(created[0]).toBe('我的预设');
    expect(created[3]).toEqual({ skills: ['image_to_text', 'uploaded'] });

    // With every skill on the block collapses back to "no opinion".
    vi.clearAllMocks();
    db.rows.length = 0;
    const fresh = await mountPanel();
    const save2 = fresh.findAllComponents({ name: 'Button' }).find(b => b.props('label') === '保存预设');
    await save2!.trigger('click');
    await flushPromises();
    await fresh.find('.dlg .it').setValue('全开预设');
    const confirm2 = fresh.findAllComponents({ name: 'Button' }).find(b => b.props('label') === '保存');
    await confirm2!.trigger('click');
    await flushPromises();
    expect(db.createPersonaPreset.mock.calls[0]![3]).toEqual({});
  });

  it('loads a preset’s agent block back into the drafts', async () => {
    db.rows.push({
      id: 7,
      name: '瘦身预设',
      content: { 'AGENTS.md': 'A', 'SOUL.md': 'S', 'USER.md': 'U' },
      character: { aiName: '艾拉', aiAvatar: '', userName: '诺亚', userAvatar: '' },
      agent: { skills: ['uploaded'] },
      createdAt: 0,
      updatedAt: 0
    });
    const { usePersonaPresets } = await import('@/composables/usePersonaPresets');
    await usePersonaPresets().refresh();

    const panel = await mountPanel();
    const row = panel.findAll('[role="button"]').find(node => node.text().includes('瘦身预设'));
    expect(row, 'saved preset row').toBeTruthy();
    await row!.trigger('click');
    await flushPromises();

    // The skills draft is the stored block: only `uploaded` stays in the index
    // — and the required media skill is a locked chip, so it has NO checkbox.
    expect(panel.find('[data-test="agent-skill-image_to_text"]').exists()).toBe(false);
    expect(panel.get('[data-test="agent-skill-locked-image_to_text"]').text()).toBe('image_to_text');
    const skillBoxes = panel.get('[data-test="agent-skills-tab"]').findAllComponents({ name: 'Checkbox' });
    expect(skillBoxes.filter(c => c.props('modelValue') === true).length).toBe(0);
    // `uploaded` is a third-party skill: its row lives in the second sub-tab.
    await panel.get('[data-test="agent-skills-scope-thirdparty"]').trigger('click');
    await flushPromises();
    const thirdParty = panel.get('[data-test="agent-skills-tab"]').findAllComponents({ name: 'Checkbox' });
    expect(thirdParty.length).toBe(1);
    expect(thirdParty[0]!.props('modelValue')).toBe(true);
  });

  it('keeps the side-by-side layout and lets the editor column scroll sideways', async () => {
    // A narrow sidebar must NOT reflow the panel (that crushed the editor) nor push
    // its rows outside: the row layout stays, the editor column is the horizontal
    // scroll container, and its children keep a readable floor.
    const panel = await mountPanel();
    const body = panel.get('[data-test="agent-skills-tab"]').element as HTMLElement;
    const root = body.closest('div[class*="md:flex-row"]') as HTMLElement | null;

    expect(root, 'the panel keeps the two-column row').not.toBeNull();
    expect(root!.querySelector('[class*="@2xl:flex-row"]')).toBeNull();

    const column = panel.get('[data-test="persona-editor-column"]');
    expect(column.classes()).toContain('overflow-x-auto');
    expect(column.classes()).toContain('min-w-0');
    // The editor keeps a floor, so the scrollbar has something to reveal.
    expect(column.find('[class*="min-w-[420px]"]').exists()).toBe(true);

    // Every agent tab body can still shrink with the column, so its truncating rows
    // do not push the column wider on their own.
    for (const tab of ['models', 'skills']) {
      const tabBody = panel.find(`[data-test="agent-${tab}-tab"]`);
      expect(tabBody.exists(), tab).toBe(true);
      expect(tabBody.classes(), tab).toContain('min-w-0');
    }
  });
});
