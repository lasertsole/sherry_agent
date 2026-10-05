/**
 * The top-bar preset button: icon-only, it names the session's preset in its
 * tooltip and opens that preset's VIEW as a right-sidebar tab in the 当前会话
 * group — there is no popup (the panel is a tab and survives outside clicks).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import SessionPresetButton from '@/pages/home/components/SessionPresetButton.vue';

const db = vi.hoisted(() => ({
  readCachedSessionPreset: vi.fn(),
  DEFAULT_CACHED_CHARACTER: {
    aiName: '橘雪莉',
    aiAvatar: '/avatar/assistant.jpg',
    userName: '远野汉娜',
    userAvatar: '/avatar/user.jpg'
  },
  GLOBAL_SESSION_KEY: '__global__',
  cacheCharacter: vi.fn(async () => undefined)
}));
vi.mock('@/composables/db', () => db);

const sidebar = vi.hoisted(() => ({ openTab: vi.fn(() => 'sessionPreset-1') }));
vi.stubGlobal(
  'useRightSidebarStore',
  () =>
    ({
      openTab: sidebar.openTab
    }) as never
);

const stubs = {
  Button: {
    name: 'Button',
    props: ['label', 'title', 'ariaLabel', 'icon', 'iconPos', 'variant', 'size'],
    emits: ['click'],
    template: `<button class="btn" :title="title" @click="$emit('click')">{{ label }}</button>`
  }
};

/**
 * Mount the button for a session.
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
    vi.clearAllMocks();
  });

  it('is icon-only and thins out with the folder button on a squeezed column', async () => {
    db.readCachedSessionPreset.mockResolvedValue({ session_id: 'sid-1', preset_id: 'coding', preset_name: '编程助手' });
    const wrapper = await mountButton();

    const trigger = wrapper.get('[data-test="session-preset-trigger"]');
    expect(trigger.text()).toBe('');
    expect(trigger.attributes('title')).toBe('当前会话预设：编程助手');
    expect(trigger.classes()).toContain('@max-[300px]:hidden!');
  });

  it('names 未选择 when the session has no binding', async () => {
    db.readCachedSessionPreset.mockResolvedValue(undefined);
    const wrapper = await mountButton('sid-old');

    expect(wrapper.get('[data-test="session-preset-trigger"]').attributes('title')).toBe('未选择');
  });

  it('opens the session preset tab in the right sidebar instead of a popup', async () => {
    db.readCachedSessionPreset.mockResolvedValue({ session_id: 'sid-1', preset_id: 'coding', preset_name: '编程助手' });
    const wrapper = await mountButton();

    await wrapper.get('[data-test="session-preset-trigger"]').trigger('click');

    expect(sidebar.openTab).toHaveBeenCalledWith('sessionPreset');
    // No popup: nothing is rendered besides the trigger.
    expect(wrapper.find('[data-test="session-preset-panel"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="toolbar-popover"]').exists()).toBe(false);
  });
});
