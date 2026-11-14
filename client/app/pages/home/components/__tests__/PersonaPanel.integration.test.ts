/**
 * The preset panel — the role config is its FIRST tab and part of the preset.
 *
 * Contract under test:
 * - the role tab leads (Character Setup before Soul / User Profile);
 * - Apply writes ROLE.md composed from both role names in the ACTIVE UI language
 *   (the role statement the agent then reads from its system prompt) alongside
 *   SOUL.md / USER.md, and persists the character to the Dexie global profile;
 * - Save Preset stores the character block with the preset, and selecting a preset
 *   restores its names;
 * - an empty role name blocks save/apply.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { mount, flushPromises, type VueWrapper } from '@vue/test-utils';
import { reactive } from 'vue';
import { locale as i18nLocale } from 'vue-i18n';
import PersonaPanel from '@/pages/home/components/PersonaPanel.vue';
import { DEFAULT_AI_AVATAR, DEFAULT_PLACEHOLDER_AVATAR, DEFAULT_USER_AVATAR } from '~/composables/defaultCharacter';

const bridge = vi.hoisted(() => ({
  readSystemPrompt: vi.fn(),
  readSystemPromptTemplate: vi.fn(),
  writeSystemPrompt: vi.fn()
}));

vi.mock('~/composables/bridge', () => bridge);

/** In-memory stand-in for the Dexie-backed helpers the panel imports. */
const db = vi.hoisted(() => {
  const rows: Array<{
    id: number;
    name: string;
    content: Record<string, string>;
    character?: Record<string, string>;
    createdAt: number;
    updatedAt: number;
  }> = [];
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
    // A fresh array per read: the shared ref only re-renders on a NEW reference.
    listPersonaPresets: vi.fn(async () => [...rows]),
    createPersonaPreset: vi.fn(
      async (name: string, content: Record<string, string>, character?: Record<string, string>) => {
        const id = rows.length + 1;
        rows.push({ id, name, content, character, createdAt: 0, updatedAt: 0 });
        return id;
      }
    ),
    updatePersonaPreset: vi.fn(
      async (id: number, content: Record<string, string>, character?: Record<string, string>) => {
        const row = rows.find(r => r.id === id);
        if (row) Object.assign(row, { content, character });
      }
    ),
    deletePersonaPreset: vi.fn(async () => undefined)
  };
});

vi.mock('@/composables/db', () => db);

const AGENTS = '# AGENTS.md\noperating instructions';
const SOUL = '# SOUL.md\nsoul body';
const USER = '# USER.md\nuser body';

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
  FileUpload: { name: 'FileUpload', template: `<div class="fu"><slot name="filelabel" :files="[]" /></div>` },
  Divider: { name: 'Divider', template: '<div class="dv" />' },
  ProgressSpinner: { name: 'ProgressSpinner', template: '<div class="spin" />' },
  Dialog: {
    name: 'Dialog',
    props: ['visible', 'header'],
    emits: ['update:visible'],
    template: `<div class="dlg" v-if="visible"><slot /><slot name="footer" /></div>`
  },
  AvatarCropDialog: { name: 'AvatarCropDialog', template: '<div class="acd" />' },
  Select: {
    name: 'Select',
    props: ['modelValue', 'options', 'optionLabel', 'optionValue'],
    emits: ['update:modelValue'],
    template: `<select class="sel" :value="modelValue" @change="$emit('update:modelValue', $event.target.value)"><option v-for="o in options" :key="o.value" :value="o.value">{{ o.label }}</option></select>`
  },
  Checkbox: {
    name: 'Checkbox',
    props: ['modelValue', 'disabled'],
    emits: ['update:modelValue'],
    template: `<button class="cb" :disabled="disabled" @click="$emit('update:modelValue', !modelValue)">C</button>`
  }
};

/** The last payload written through `writeSystemPrompt` (the apply verification reads it back). */
let written: Record<string, string> | null = null;

/**
 * An agent-config store double whose catalogue carries the skill sets the two
 * restricted built-in presets derive their agent blocks from.
 * @param catalogue
 * @param catalogue.skills
 */
function makeCatalogStore(catalogue?: {
  skills?: Array<{ name: string; builtin: boolean; description?: string; required?: boolean }>;
}) {
  const skills = catalogue?.skills ?? [
    { name: 'alpha', builtin: true, description: 'Built-in skill.' },
    { name: 'uploaded', builtin: false, description: 'Third-party skill.' }
  ];
  return reactive({
    catalog: { subagent_roles: [], skills },
    catalogLoaded: true,
    subagentRoles: [],
    skills: {
      builtin: skills.filter(skill => skill.builtin),
      thirdParty: skills.filter(skill => !skill.builtin)
    },
    loadCatalog: async () => {},
    hydrate: async () => {},
    configOf: () => ({}),
    selectedSkills: () => null,
    isPending: () => false,
    save: async () => {}
  });
}

/**
 * A catalogue wide enough for EVERY built-in's derivation (the work-flow
 * skills), installed for the whole suite: a preset click throws without one,
 * and these tests are not about the derivation.
 */
function makeWideCatalogStore() {
  return makeCatalogStore({
    skills: [
      { name: 'image_to_text', builtin: true, required: true },
      { name: 'speech_to_text', builtin: true, required: true },
      { name: 'text_to_image', builtin: true, required: true },
      { name: 'video_text_to_text', builtin: true, required: true },
      { name: 'clawhub', builtin: true },
      { name: 'code-wiki', builtin: true }
    ]
  });
}

async function mountPanel() {
  const wrapper = mount(PersonaPanel, { global: { stubs } });
  await flushPromises();
  return wrapper;
}

/**
 * The stub button carrying the given visible label.
 * @param wrapper Mounted panel wrapper.
 * @param text Visible button label.
 * @returns The matching button wrapper.
 */
function buttonByText(wrapper: VueWrapper, text: string) {
  const button = wrapper.findAll('button').find(b => b.text() === text);
  if (!button) throw new Error(`no button labelled ${text}`);
  return button;
}

describe('PersonaPanel role tab', () => {
  beforeEach(() => {
    db.rows.length = 0;
    written = null;
    vi.clearAllMocks();
    bridge.readSystemPrompt.mockImplementation(
      async () => written ?? { 'AGENTS.md': AGENTS, 'SOUL.md': SOUL, 'USER.md': USER }
    );
    bridge.readSystemPromptTemplate.mockResolvedValue({
      'AGENTS.md': 'TPL-AGENTS',
      'SOUL.md': 'TPL-SOUL',
      'USER.md': 'TPL-USER'
    });
    vi.stubGlobal('useAgentConfigStore', () => makeWideCatalogStore());
    bridge.writeSystemPrompt.mockImplementation(async (map: Record<string, string>) => {
      written = map;
    });
  });

  afterEach(() => {
    i18nLocale.value = 'zh';
  });

  it('leads with the role tab, before the soul and user files', async () => {
    const wrapper = await mountPanel();

    const headers = wrapper.findAllComponents({ name: 'TabPanel' }).map(c => c.props('header'));
    // Role first, the two editable persona files, then the agent-config tabs
    // (Agent models / Skills) the preset also carries. AGENTS.md is not a tab.
    expect(headers).toEqual(['角色配置', '人格灵魂', '用户信息', '代理模型', '技能', '高级']);
  });

  it('composes ROLE.md from both role names on 应用 and persists the character', async () => {
    const wrapper = await mountPanel();
    await wrapper.get('[data-test="persona-role-ai-name"]').setValue('小樱');
    await wrapper.get('[data-test="persona-role-user-name"]').setValue('小明');

    await buttonByText(wrapper, '应用').trigger('click');
    await flushPromises();

    expect(bridge.writeSystemPrompt).toHaveBeenCalledTimes(1);
    const payload = bridge.writeSystemPrompt.mock.calls[0]![0] as Record<string, string>;
    expect(payload['ROLE.md']).toBe('# ROLE.md\n\n你将扮演小樱。\n用户将扮演小明。\n');
    // Only the editable persona files are written; AGENTS.md cannot be edited.
    expect(payload).not.toHaveProperty('AGENTS.md');
    expect(payload['SOUL.md']).toBe(SOUL);
    expect(payload['USER.md']).toBe(USER);

    expect(db.cacheCharacter).toHaveBeenCalledWith({
      session_id: '__global__',
      aiName: '小樱',
      aiAvatar: '/avatar/assistant.jpg',
      userName: '小明',
      userAvatar: '/avatar/user.jpg'
    });
    expect(wrapper.emitted('saved')).toBeTruthy();
  });

  it('writes the role statement in the active UI language', async () => {
    i18nLocale.value = 'en';
    const wrapper = await mountPanel();
    await wrapper.get('[data-test="persona-role-ai-name"]').setValue('Sakura');
    await wrapper.get('[data-test="persona-role-user-name"]').setValue('Akira');

    // The whole panel follows the locale, buttons included.
    await buttonByText(wrapper, 'Apply').trigger('click');
    await flushPromises();

    const payload = bridge.writeSystemPrompt.mock.calls[0]![0] as Record<string, string>;
    expect(payload['ROLE.md']).toBe('# ROLE.md\n\nYou will play Sakura.\nThe user will play Akira.\n');
  });

  it('keeps save/apply enabled with an empty user role (no role-play is legal)', async () => {
    const wrapper = await mountPanel();
    await wrapper.get('[data-test="persona-role-user-name"]').setValue('');

    expect(buttonByText(wrapper, '应用').attributes('disabled')).toBeUndefined();
    expect(buttonByText(wrapper, '保存预设').attributes('disabled')).toBeUndefined();
  });

  it('still blocks save/apply past the char limit', async () => {
    // An oversized file loaded from the backend (e.g. hand-edited) must not be re-saved.
    const huge = 'x'.repeat(2500);
    bridge.readSystemPrompt.mockResolvedValueOnce({ 'SOUL.md': huge, 'USER.md': USER, 'AGENTS.md': AGENTS });
    const wrapper = await mountPanel();

    expect(buttonByText(wrapper, '应用').attributes('disabled')).toBeDefined();
  });

  it('lists the two built-ins in catalogue order', async () => {
    const wrapper = await mountPanel();

    const builtinRows = wrapper.findAll('[data-test^="builtin-"]');
    expect(builtinRows.map(row => row.attributes('data-test'))).toEqual(['builtin-pure', 'builtin-sherry']);
    // Non-deletable: the virtual rows never carry a delete button.
    for (const row of builtinRows) expect(row.findAll('button')).toHaveLength(0);
  });

  it('loads the full template for the 橘雪莉 built-in', async () => {
    const wrapper = await mountPanel();
    await wrapper.get('[data-test="builtin-sherry"]').trigger('click');
    await flushPromises();

    const textareas = wrapper.findAll('textarea').map(t => (t.element as HTMLTextAreaElement).value);
    // SOUL + USER tabs; the built-in ships no user profile.
    expect(textareas).toEqual(['TPL-SOUL', '']);
    // AGENTS.md is not exposed as an editable tab.
    expect(wrapper.text()).not.toContain('运行守则');
    expect((wrapper.get('[data-test="persona-role-ai-name"]').element as HTMLInputElement).value).toBe('橘雪莉');
  });

  it('loads 纯净 as an empty persona', async () => {
    vi.stubGlobal('useAgentConfigStore', () => makeCatalogStore());
    const wrapper = await mountPanel();

    await wrapper.get('[data-test="builtin-pure"]').trigger('click');
    await flushPromises();

    // Nobody named and NO persona content.
    const textareas = wrapper.findAll('textarea').map(t => (t.element as HTMLTextAreaElement).value);
    expect(textareas).toEqual(['', '']);
    // The operating rules are still written by the preset, just not displayed.
    expect(bridge.readSystemPromptTemplate).toHaveBeenCalled();
    expect((wrapper.get('[data-test="persona-role-ai-name"]').element as HTMLInputElement).value).toBe('');
    expect(wrapper.get('[alt="assistant avatar"]').attributes('src')).toBe(DEFAULT_PLACEHOLDER_AVATAR);
    // The tool / middleware tabs are gone: neither is configurable.
    expect(wrapper.find('[data-test="agent-tools-tab"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="agent-middlewares-tab"]').exists()).toBe(false);
  });

  it('saves the role config with the preset and restores it on selection', async () => {
    const wrapper = await mountPanel();
    await wrapper.get('[data-test="persona-role-ai-name"]').setValue('小樱');

    await buttonByText(wrapper, '保存预设').trigger('click');
    await flushPromises();
    await wrapper.get('.dlg input').setValue('我的预设');
    await buttonByText(wrapper, '保存').trigger('click');
    await flushPromises();

    expect(db.createPersonaPreset).toHaveBeenCalledTimes(1);
    const [name, content, character] = db.createPersonaPreset.mock.calls[0]!;
    expect(name).toBe('我的预设');
    // The preset stores the edited files; ROLE.md is recomposed on apply.
    expect(content).toEqual({ 'SOUL.md': SOUL, 'USER.md': USER });
    expect(character).toEqual({
      aiName: '小樱',
      aiAvatar: '/avatar/assistant.jpg',
      userName: '远野汉娜',
      userAvatar: '/avatar/user.jpg'
    });

    // A second preset carries its own roles; selecting it restores them.
    db.rows.push({
      id: 99,
      name: '预设A',
      content: { 'SOUL.md': 'S2', 'USER.md': 'U2' },
      character: { aiName: '艾拉', aiAvatar: '/a.png', userName: '诺亚', userAvatar: '/u.png' },
      createdAt: 0,
      updatedAt: 0
    });
    const { usePersonaPresets } = await import('@/composables/usePersonaPresets');
    await usePersonaPresets().refresh();
    await flushPromises();

    const row = wrapper.findAll('[role="button"]').find(el => el.text().includes('预设A'));
    expect(row, 'preset row').toBeTruthy();
    await row!.trigger('click');
    await flushPromises();

    expect((wrapper.get('[data-test="persona-role-ai-name"]').element as HTMLInputElement).value).toBe('艾拉');
    expect((wrapper.get('[data-test="persona-role-user-name"]').element as HTMLInputElement).value).toBe('诺亚');
  });

  it('resets one avatar to the neutral placeholder on 重置头像, leaving the name alone', async () => {
    const wrapper = await mountPanel();
    // A fresh panel carries the shipped default photos.
    expect(wrapper.get('[alt="assistant avatar"]').attributes('src')).toBe(DEFAULT_AI_AVATAR);
    expect(wrapper.get('[alt="user avatar"]').attributes('src')).toBe(DEFAULT_USER_AVATAR);

    await wrapper.get('[data-test="persona-role-ai-avatar-reset"]').trigger('click');

    // The cleared field renders the gray silhouette and the button turns off.
    expect(wrapper.get('[alt="assistant avatar"]').attributes('src')).toBe(DEFAULT_PLACEHOLDER_AVATAR);
    expect(wrapper.get('[data-test="persona-role-ai-avatar-reset"]').attributes('disabled')).toBeDefined();
    // Only the avatar moved: the name and the other role are untouched.
    expect((wrapper.get('[data-test="persona-role-ai-name"]').element as HTMLInputElement).value).toBe('橘雪莉');
    expect(wrapper.get('[alt="user avatar"]').attributes('src')).toBe(DEFAULT_USER_AVATAR);
  });

  it('prefills an explicitly empty role name as empty (a nameless preset)', async () => {
    db.readCachedCharacter.mockImplementation(async () => ({
      aiName: '',
      aiAvatar: '',
      userName: '',
      userAvatar: ''
    }));

    const wrapper = await mountPanel();

    // The inputs stay empty (their placeholder shows); substituting the shipped 橘雪莉/远野汉娜 names
    // here made the panel disagree with the preset it had just applied.
    expect((wrapper.get('[data-test="persona-role-ai-name"]').element as HTMLInputElement).value).toBe('');
    expect((wrapper.get('[data-test="persona-role-user-name"]').element as HTMLInputElement).value).toBe('');
  });

  it('resets both role names to the built-in defaults with 恢复默认', async () => {
    const wrapper = await mountPanel();
    await wrapper.get('[data-test="persona-role-ai-name"]').setValue('小樱');
    await wrapper.get('[data-test="persona-role-user-name"]').setValue('小明');

    // The role tab's restore button is the first Restore Default (the file tabs carry one each).
    await buttonByText(wrapper, '恢复默认').trigger('click');
    await flushPromises();

    expect((wrapper.get('[data-test="persona-role-ai-name"]').element as HTMLInputElement).value).toBe('橘雪莉');
    expect((wrapper.get('[data-test="persona-role-user-name"]').element as HTMLInputElement).value).toBe('远野汉娜');
  });
});
