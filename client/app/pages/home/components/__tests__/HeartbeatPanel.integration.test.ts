/**
 * The 心跳 panel's global switch: it mirrors the backend state, applies the
 * toggle through the backend (which starts/stops the running scheduler and
 * persists the choice), and rolls back when the backend refuses — a switch that
 * kept a state the backend never applied would misreport what is running.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount, flushPromises, type VueWrapper } from '@vue/test-utils';
import HeartbeatPanel from '@/pages/home/components/HeartbeatPanel.vue';

const bridge = vi.hoisted(() => ({
  readHeartbeat: vi.fn(),
  writeHeartbeat: vi.fn(),
  fetchHeartbeatStatus: vi.fn(),
  setHeartbeatEnabled: vi.fn()
}));

vi.mock('~/composables/bridge', () => bridge);

const FILE = '# Heartbeat Tasks\n\n## Active Tasks\n\n- task A\n\n## Completed\n';

const stubs = {
  Button: {
    props: ['label', 'disabled', 'loading'],
    emits: ['click'],
    template: '<button class="btn" :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>'
  },
  Textarea: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<textarea class="ta" :value="modelValue" />'
  },
  ProgressSpinner: { template: '<div class="spinner" />' },
  ToggleSwitch: {
    props: ['modelValue', 'disabled'],
    emits: ['update:modelValue'],
    template:
      '<button class="switch" :class="{ on: modelValue }" :disabled="disabled" @click="$emit(\'update:modelValue\', !modelValue)"></button>'
  }
};

/** Mount the panel with both loads answered. */
async function mountPanel() {
  const wrapper = mount(HeartbeatPanel, { global: { stubs } });
  await flushPromises();
  return wrapper;
}

/**
 * The switch element of a mounted panel.
 * @param wrapper
 */
const switchEl = (wrapper: VueWrapper) => wrapper.find('button.switch');

describe('HeartbeatPanel global switch', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    bridge.readHeartbeat.mockResolvedValue({ 'HEARTBEAT.md': FILE });
    bridge.fetchHeartbeatStatus.mockResolvedValue({ enabled: true, running: true, interval_s: 1800 });
    bridge.setHeartbeatEnabled.mockImplementation(async (enabled: boolean) => ({
      enabled,
      running: enabled,
      interval_s: 1800
    }));
  });

  it('shows the state the backend reported, with the tick interval', async () => {
    const wrapper = await mountPanel();

    expect(switchEl(wrapper).classes()).toContain('on');
    expect(wrapper.text()).toContain('全局心跳');
    expect(wrapper.text()).toContain('30 分钟');
  });

  it('turns the heartbeat off through the backend and shows the off hint', async () => {
    const wrapper = await mountPanel();

    await switchEl(wrapper).trigger('click');
    await flushPromises();

    expect(bridge.setHeartbeatEnabled).toHaveBeenCalledWith(false);
    expect(switchEl(wrapper).classes()).not.toContain('on');
    expect(wrapper.text()).toContain('已关闭');
  });

  it('rolls back when the backend refuses the toggle', async () => {
    bridge.setHeartbeatEnabled.mockRejectedValueOnce(new Error('config write failed'));
    const wrapper = await mountPanel();

    await switchEl(wrapper).trigger('click');
    await flushPromises();

    // Back to the state the backend still has.
    expect(switchEl(wrapper).classes()).toContain('on');
    expect(wrapper.text()).not.toContain('已关闭');
  });

  it('keeps the switch inert until the state has been read', async () => {
    let release: (value: { enabled: boolean; running: boolean; interval_s: number }) => void = () => {};
    bridge.fetchHeartbeatStatus.mockReturnValueOnce(
      new Promise(resolve => {
        release = resolve;
      })
    );
    const wrapper = mount(HeartbeatPanel, { global: { stubs } });
    await flushPromises();

    expect(switchEl(wrapper).attributes('disabled')).toBeDefined();

    release({ enabled: false, running: false, interval_s: 1800 });
    await flushPromises();

    expect(switchEl(wrapper).attributes('disabled')).toBeUndefined();
    expect(wrapper.text()).toContain('已关闭');
  });
});
