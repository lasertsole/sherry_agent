/**
 * The mandatory new-session dialog: a session is created only after a preset is
 * chosen and applied, so this suite pins the two halves of that contract — the
 * catalogue it offers (编程助手 first, preselected) and the creation sequence
 * (apply → bind → character snapshot → placeholder → navigate).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import NewSessionPresetDialog from '@/pages/home/components/NewSessionPresetDialog.vue';
import type { PersonaPreset } from '@/composables/db';

const bridge = vi.hoisted(() => ({
  readSystemPrompt: vi.fn(),
  readSystemPromptTemplate: vi.fn(),
  writeSystemPrompt: vi.fn()
}));
vi.mock('~/composables/bridge', () => bridge);

const mitt = vi.hoisted(() => ({ emit: vi.fn(), on: vi.fn(), off: vi.fn() }));
vi.mock('@/composables/mitt', () => mitt);

const sidebar = vi.hoisted(() => ({ ensureSessionCharacter: vi.fn(async () => undefined) }));
vi.mock('@/pages/home/components/SessionSidebar.vue', () => sidebar);

const db = vi.hoisted(() => {
  const presets: PersonaPreset[] = [];
  return {
    presets,
    DEFAULT_CACHED_CHARACTER: {
      aiName: '橘雪莉',
      aiAvatar: '/avatar/assistant.jpg',
      userName: '远野汉娜',
      userAvatar: '/avatar/user.jpg'
    },
    GLOBAL_SESSION_KEY: '__global__',
    cacheCharacter: vi.fn(async () => undefined),
    cacheSessionMeta: vi.fn(async () => undefined),
    cacheSessionPreset: vi.fn(async () => undefined),
    listPersonaPresets: vi.fn(async () => [...presets])
  };
});
vi.mock('@/composables/db', () => db);

const router = vi.hoisted(() => ({ push: vi.fn(async () => {}) }));
vi.stubGlobal('useRouter', () => router);

const TEMPLATE = {
  'AGENTS.md': '# AGENTS.md\nrules',
  'SOUL.md': '# SOUL.md\nsoul',
  'USER.md': '# USER.md\nuser'
};

const stubs = {
  Dialog: {
    name: 'Dialog',
    props: ['visible', 'header', 'modal', 'closable', 'dismissableMask', 'style'],
    emits: ['update:visible', 'hide'],
    template: '<div class="dlg" v-if="visible"><slot /><slot name="footer" /></div>'
  },
  Button: {
    name: 'Button',
    props: ['label', 'disabled', 'loading'],
    emits: ['click'],
    template: `<button class="btn" :disabled="disabled" @click="$emit('click')">{{ label }}</button>`
  }
};

/**
 * Mount the dialog open, with the writes answered.
 * @returns The wrapper and the map the apply wrote.
 */
async function mountDialog() {
  const written: Record<string, string> = {};
  bridge.writeSystemPrompt.mockImplementation(async (map: Record<string, string>) => {
    Object.assign(written, map);
  });
  bridge.readSystemPrompt.mockImplementation(async () => ({ ...written, ...TEMPLATE }));
  bridge.readSystemPromptTemplate.mockResolvedValue(TEMPLATE);
  const wrapper = mount(NewSessionPresetDialog, {
    props: { visible: true },
    global: { stubs }
  });
  await flushPromises();
  return { wrapper, written };
}

/**
 * The stub button carrying the given visible label.
 * @param wrapper Mounted dialog wrapper.
 * @param text Visible button label.
 * @returns The matching button wrapper.
 */
function buttonByText(wrapper: ReturnType<typeof mount>, text: string) {
  const button = wrapper.findAll('button').find(candidate => candidate.text() === text);
  if (!button) throw new Error(`no button labelled ${text}`);
  return button;
}

describe('NewSessionPresetDialog', () => {
  beforeEach(() => {
    db.presets.length = 0;
    vi.clearAllMocks();
  });

  it('lists the catalogue with 编程助手 first and preselected', async () => {
    db.presets.push({
      id: 7,
      name: '我的预设',
      content: { 'AGENTS.md': 'A', 'SOUL.md': 'S', 'USER.md': 'U' },
      createdAt: 0,
      updatedAt: 0
    });
    const { wrapper } = await mountDialog();

    const rows = wrapper.findAll('[data-test^="new-session-preset-option-"]');
    expect(rows.map(row => row.attributes('data-test'))).toEqual([
      'new-session-preset-option-coding',
      'new-session-preset-option-sherry',
      'new-session-preset-option-user:7'
    ]);
    // The default preset is preselected, so 创建会话 is immediately actionable.
    expect(rows[0]!.find('i.pi-check').exists()).toBe(true);
    expect(buttonByText(wrapper, '创建会话').attributes('disabled')).toBeUndefined();
  });

  it('applies the chosen preset, binds and creates the session, then announces it', async () => {
    const { wrapper, written } = await mountDialog();

    await wrapper.get('[data-test="new-session-preset-option-sherry"]').trigger('click');
    await buttonByText(wrapper, '创建会话').trigger('click');
    await flushPromises();

    // 1. The preset was applied (files written + verified) …
    expect(written['AGENTS.md']).toBe(TEMPLATE['AGENTS.md']);
    expect(written['SOUL.md']).toBe(TEMPLATE['SOUL.md']);
    expect(written['USER.md']).toBe(TEMPLATE['USER.md']);
    expect(written['ROLE.md']).toBe('# ROLE.md\n\n你将扮演橘雪莉。\n用户将扮演远野汉娜。\n');
    expect(db.cacheCharacter).toHaveBeenCalledWith({
      session_id: '__global__',
      aiName: '橘雪莉',
      aiAvatar: '/avatar/assistant.jpg',
      userName: '远野汉娜',
      userAvatar: '/avatar/user.jpg'
    });

    // 2. … the binding, the character snapshot and the placeholder were created
    //    for the SAME session id …
    const sessionId = (db.cacheSessionPreset.mock.calls[0]![0] as { session_id: string }).session_id;
    expect(sessionId).toBeTruthy();
    expect(db.cacheSessionPreset).toHaveBeenCalledWith({
      session_id: sessionId,
      preset_id: 'sherry',
      preset_name: '橘雪莉'
    });
    expect(sidebar.ensureSessionCharacter).toHaveBeenCalledWith(sessionId);
    expect((db.cacheSessionMeta.mock.calls[0]![0] as { id: string }).id).toBe(sessionId);

    // 3. … the sidebar was told and the app navigated to it, dialog closed.
    expect(mitt.emit).toHaveBeenCalledWith('session:created', expect.objectContaining({ id: sessionId }));
    expect(router.push).toHaveBeenCalledWith(`/home/${sessionId}`);
    expect(wrapper.emitted('update:visible')?.at(-1)).toEqual([false]);
  });

  it('keeps the dialog open when the apply cannot be verified', async () => {
    const { wrapper } = await mountDialog();
    // The read-back does not match what was written → the apply fails.
    bridge.readSystemPrompt.mockResolvedValue({ 'AGENTS.md': 'something else' });

    await buttonByText(wrapper, '创建会话').trigger('click');
    await flushPromises();

    expect(wrapper.emitted('update:visible')).toBeUndefined();
    expect(db.cacheSessionPreset).not.toHaveBeenCalled();
    expect(db.cacheSessionMeta).not.toHaveBeenCalled();
    expect(router.push).not.toHaveBeenCalled();
  });
});
