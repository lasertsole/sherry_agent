/**
 * The session preset VIEW (a right-sidebar tab under 当前会话): it names the
 * preset the active session was created with and renders that preset's content
 * in the same four tabs the 预设 panel edits — strictly view-only, no other
 * preset selectable or previewable.
 */
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { locale as i18nLocale } from 'vue-i18n';
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

const stubs = {
  Divider: { name: 'Divider', template: '<div class="dv" />' },
  ProgressSpinner: { name: 'ProgressSpinner', template: '<div class="spin" />' },
  TabView: { name: 'TabView', props: ['activeIndex'], template: '<div class="tv"><slot /></div>' },
  TabPanel: { name: 'TabPanel', props: ['value', 'header'], template: '<div class="tp"><slot /></div>' }
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
