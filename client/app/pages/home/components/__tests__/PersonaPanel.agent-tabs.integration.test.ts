/**
 * The 预设 panel's three agent-config tabs (工具 / 中间件 / 子代理模型).
 *
 * Contract under test: the tabs render from the BACKEND catalogue (no
 * hardcoded names), the drafts map to the `agent` block (all-on / no-opinion
 * collapses to `{}`), and 保存预设 stores the block while 应用 writes it to the
 * open session through the store.
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

/** The default catalogue: two optional tools in an optional group + terminal. */
const DEFAULT_TOOLS = [
  { name: 'read_file', group: 'files' },
  { name: 'write_file', group: 'files' },
  { name: 'terminal', group: 'terminal' }
];

const DEFAULT_GROUPS = [
  {
    group: 'files',
    tools: [
      { name: 'read_file', group: 'files', description: 'Read a file with pagination.' },
      { name: 'write_file', group: 'files', description: 'Write file to disk.' }
    ]
  },
  {
    group: 'terminal',
    tools: [{ name: 'terminal', group: 'terminal', description: 'Run shell commands.' }]
  }
];

/**
 * A functional store double: a real catalogue + the drafts a save receives.
 * @param overrides Per-test overrides (hydrated config, a custom catalogue).
 * @param overrides.config
 * @param overrides.tools
 * @param overrides.groups
 */
function makeAgentStore(
  overrides: {
    config?: Record<string, unknown>;
    tools?: Array<Record<string, unknown>>;
    groups?: Array<{ group: string; tools: Array<Record<string, unknown>> }>;
  } = {}
) {
  const saved: Array<Record<string, unknown>> = [];
  const tools = overrides.tools ?? DEFAULT_TOOLS;
  const store = reactive({
    catalog: {
      tools,
      middlewares: [
        { name: 'HumanInTheLoop', required: true, gateable: false },
        { name: 'TaskIntentMiddleware', required: false, gateable: true }
      ],
      subagent_roles: [{ role: 'researcher', model_tier: 'auxiliary', description: 'r' }]
    },
    catalogLoaded: true,
    toolGroups: overrides.groups ?? DEFAULT_GROUPS,
    middlewares: {
      gateable: [{ name: 'TaskIntentMiddleware', required: false, gateable: true }],
      locked: [{ name: 'HumanInTheLoop', required: true, gateable: false }]
    },
    subagentRoles: [{ role: 'researcher', model_tier: 'auxiliary', description: 'r' }],
    loadCatalog: vi.fn(async () => undefined),
    hydrate: vi.fn(async () => undefined),
    configOf: () => (overrides.config ?? {}) as never,
    enabledTools: () => tools.map(tool => tool.name) as never,
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

  it('renders the three tabs from the backend catalogue without hardcoding names', async () => {
    const panel = await mountPanel();

    const headers = panel.findAllComponents({ name: 'TabPanel' }).map(c => c.props('header'));
    expect(headers.slice(-4)).toEqual(['工具', '中间件', '子代理模型', '技能']);

    // Tools grouped, every catalogue tool present with a checkbox…
    expect(panel.find('[data-test="agent-tool-read_file"]').exists()).toBe(true);
    expect(panel.find('[data-test="agent-tool-terminal"]').exists()).toBe(true);
    // …and each row carries the backend description as its hover tooltip.
    const row = panel.get('[data-test="agent-tool-read_file"]').element.closest('label');
    expect(row?.getAttribute('title')).toBe('Read a file with pagination.');
    // …the gateable middleware has a switch and the required one is LOCKED (no
    // switch, listed under the locked set instead).
    expect(panel.find('[data-test="agent-middleware-TaskIntentMiddleware"]').exists()).toBe(true);
    expect(panel.find('[data-test="agent-middleware-HumanInTheLoop"]').exists()).toBe(false);
    expect(panel.text()).toContain('HumanInTheLoop');
    // The role picker offers the role + its tier state.
    expect(panel.find('[data-test="agent-role-model-researcher"]').exists()).toBe(true);
  });

  it('stores the draft in a saved preset and collapses "all on" to an empty block', async () => {
    const panel = await mountPanel();

    // Uncheck one tool and turn the gateable middleware off.
    await panel.get('[data-test="agent-tool-terminal"]').trigger('click');
    await panel.get('[data-test="agent-middleware-TaskIntentMiddleware"]').trigger('click');
    await flushPromises();

    // 保存预设 (no preset being edited → the name dialog opens) → confirm.
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
    expect(created[3]).toEqual({
      tools: ['read_file', 'write_file'],
      middlewares_disabled: ['TaskIntentMiddleware']
    });
  });

  it('loads a preset’s agent block back into the drafts', async () => {
    db.rows.push({
      id: 7,
      name: '瘦身预设',
      content: { 'AGENTS.md': 'A', 'SOUL.md': 'S', 'USER.md': 'U' },
      character: { aiName: '艾拉', aiAvatar: '', userName: '诺亚', userAvatar: '' },
      agent: { tools: ['terminal'], middlewares_disabled: ['TaskIntentMiddleware'] },
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

    // Only `terminal` stays checked; the middleware switch sits OFF.
    const checkboxes = panel.findAllComponents({ name: 'Checkbox' });
    expect(checkboxes.length).toBe(3);
    const checked = checkboxes.filter(c => c.props('modelValue') === true);
    expect(checked.length).toBe(1);
  });

  it('locks required tools on and moves a bulk-only group as a whole', async () => {
    agentStore.state = makeAgentStore({
      tools: [
        { name: 'read_file', group: 'files', required: true },
        { name: 'terminal', group: 'terminal' },
        { name: 'taskflow_create', group: 'tasks' },
        { name: 'todowrite', group: 'tasks' }
      ],
      groups: [
        {
          group: 'files',
          tools: [{ name: 'read_file', group: 'files', description: 'Read a file.', required: true }]
        },
        {
          group: 'terminal',
          tools: [{ name: 'terminal', group: 'terminal', description: 'Run shell commands.' }]
        },
        {
          group: 'tasks',
          tools: [
            { name: 'taskflow_create', group: 'tasks', description: 'Create a flow.' },
            { name: 'todowrite', group: 'tasks', description: 'Write todos.' }
          ]
        }
      ]
    }).store;
    const panel = await mountPanel();

    // Required row: locked ON (disabled + checked) with a lock glyph and the hint.
    const requiredBox = panel.get('[data-test="agent-tool-read_file"]');
    expect(requiredBox.attributes('disabled')).toBeDefined();
    expect(requiredBox.attributes('data-checked')).toBe('true');
    const requiredRow = requiredBox.element.closest('label');
    expect(requiredRow?.querySelector('.pi-lock')).not.toBeNull();
    expect(requiredRow?.getAttribute('title')).toBe('Read a file.\n必需，不可取消');
    // Clicking it changes nothing (the draft never drops a required tool).
    await requiredBox.trigger('click');
    await flushPromises();
    expect(panel.get('[data-test="agent-tool-read_file"]').attributes('data-checked')).toBe('true');
    // A fully required group has no select-all toggle — a locked hint instead.
    expect(panel.find('[data-test="agent-tool-group-files"]').exists()).toBe(false);
    expect(panel.find('[data-test="agent-tool-group-locked-files"]').exists()).toBe(true);
    // A bulk-only group carries NO per-tool switch, only chips + the group toggle.
    expect(panel.find('[data-test="agent-tool-taskflow_create"]').exists()).toBe(false);
    expect(panel.find('[data-test="agent-tool-bulk-taskflow_create"]').exists()).toBe(true);
    expect(panel.find('[data-test="agent-tool-group-tasks"]').exists()).toBe(true);

    // 全部禁用 keeps the required tool on and leaves the rest off.
    await panel.get('[data-test="agent-tools-none"]').trigger('click');
    await flushPromises();
    expect(panel.get('[data-test="agent-tool-read_file"]').attributes('data-checked')).toBe('true');
    expect(panel.get('[data-test="agent-tool-terminal"]').attributes('data-checked')).toBe('false');
    const chip = () => panel.get('[data-test="agent-tool-bulk-taskflow_create"]').classes();
    expect(chip()).toContain('line-through');

    // The group toggle selects / clears the whole membership (chips mirror it).
    await panel.get('[data-test="agent-tool-group-tasks"]').trigger('click');
    await flushPromises();
    expect(chip()).not.toContain('line-through');
    await panel.get('[data-test="agent-tool-group-tasks"]').trigger('click');
    await flushPromises();
    expect(chip()).toContain('line-through');

    // A draft can never be saved with a required tool missing (the backend would
    // refuse the payload): clearing everything still stores the required set.
    const saveButton = panel.findAllComponents({ name: 'Button' }).find(b => b.props('label') === '保存预设');
    await saveButton!.trigger('click');
    await flushPromises();
    await panel.find('.dlg .it').setValue('仅必需');
    const confirm = panel.findAllComponents({ name: 'Button' }).find(b => b.props('label') === '保存');
    await confirm!.trigger('click');
    await flushPromises();
    expect(db.createPersonaPreset.mock.calls[0]![3]).toEqual({ tools: ['read_file'] });
  });
});
