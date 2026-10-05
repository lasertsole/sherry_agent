/**
 * The top-bar preset button: it names the session's bound preset and its popup
 * lists the catalogue and previews the selected preset's content in the same
 * four tabs the 预设 panel edits (view-only).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import SessionPresetButton from '@/pages/home/components/SessionPresetButton.vue';
import type { PersonaPreset } from '@/composables/db';

const bridge = vi.hoisted(() => ({
  readSystemPrompt: vi.fn(),
  readSystemPromptTemplate: vi.fn(),
  writeSystemPrompt: vi.fn()
}));
vi.mock('~/composables/bridge', () => bridge);

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
  Button: {
    name: 'Button',
    props: ['label', 'title', 'ariaLabel', 'icon', 'iconPos', 'variant', 'size'],
    emits: ['click'],
    template: `<button class="btn" :title="title" @click="$emit('click')">{{ label }}</button>`
  },
  Divider: { name: 'Divider', template: '<div class="dv" />' },
  ProgressSpinner: { name: 'ProgressSpinner', template: '<div class="spin" />' },
  TabView: { name: 'TabView', props: ['activeIndex'], template: '<div class="tv"><slot /></div>' },
  TabPanel: { name: 'TabPanel', props: ['value', 'header'], template: '<div class="tp"><slot /></div>' }
};

/**
 * Mount the button for a session and open its popup.
 * @param sessionId Session the button sits above.
 * @returns The mounted wrapper.
 */
async function mountButton(sessionId = 'sid-1') {
  const wrapper = mount(SessionPresetButton, {
    props: { sessionId },
    global: { stubs }
  });
  await flushPromises();
  return wrapper;
}

describe('SessionPresetButton', () => {
  beforeEach(() => {
    db.presets.length = 0;
    vi.clearAllMocks();
    bridge.readSystemPromptTemplate.mockResolvedValue(TEMPLATE);
  });

  it('names the bound preset and previews it in the four content tabs', async () => {
    db.readCachedSessionPreset.mockResolvedValue({ session_id: 'sid-1', preset_id: 'coding', preset_name: '编程助手' });
    const wrapper = await mountButton();

    expect(wrapper.get('[data-test="session-preset-trigger"]').text()).toBe('编程助手');

    await wrapper.get('[data-test="session-preset-trigger"]').trigger('click');
    await flushPromises();

    const panel = wrapper.get('[data-test="session-preset-panel"]');
    expect(panel.get('[data-test="session-preset-bound"]').text()).toBe('编程助手');
    // Catalogue order: 编程助手, 橘雪莉, then the saved presets.
    expect(panel.findAll('[data-test^="session-preset-entry-"]').map(row => row.attributes('data-test'))).toEqual([
      'session-preset-entry-coding',
      'session-preset-entry-sherry'
    ]);
    // Preview: the operating rules, and 空 for the coding preset's blank soul / user profile.
    expect(panel.get('[data-test="session-preset-content-AGENTS.md"]').text()).toBe(TEMPLATE['AGENTS.md']);
    expect(panel.get('[data-test="session-preset-content-SOUL.md"]').text()).toBe('（空）');
    expect(panel.get('[data-test="session-preset-content-USER.md"]').text()).toBe('（空）');
  });

  it('reads 未选择 for a session created before the binding existed, and previews the default', async () => {
    db.readCachedSessionPreset.mockResolvedValue(undefined);
    const wrapper = await mountButton('sid-old');

    expect(wrapper.get('[data-test="session-preset-trigger"]').text()).toBe('未选择');

    await wrapper.get('[data-test="session-preset-trigger"]').trigger('click');
    await flushPromises();

    expect(wrapper.get('[data-test="session-preset-panel"]').text()).toContain('未选择');
    // No binding: the preview falls back to the default preset.
    expect(wrapper.get('[data-test="session-preset-content-AGENTS.md"]').text()).toBe(TEMPLATE['AGENTS.md']);
  });

  it('previews another preset on click without touching the session', async () => {
    db.readCachedSessionPreset.mockResolvedValue({ session_id: 'sid-1', preset_id: 'coding', preset_name: '编程助手' });
    const wrapper = await mountButton();
    await wrapper.get('[data-test="session-preset-trigger"]').trigger('click');
    await flushPromises();

    await wrapper.get('[data-test="session-preset-entry-sherry"]').trigger('click');
    await flushPromises();

    expect(wrapper.get('[data-test="session-preset-content-SOUL.md"]').text()).toBe(TEMPLATE['SOUL.md']);
    // View-only: neither the persona files nor the global character were written.
    expect(bridge.writeSystemPrompt).not.toHaveBeenCalled();
    expect(db.cacheCharacter).not.toHaveBeenCalled();
    // The session's own binding is still the header value.
    expect(wrapper.get('[data-test="session-preset-bound"]').text()).toBe('编程助手');
  });
});
