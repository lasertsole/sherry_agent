import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount } from '@vue/test-utils';
import ModeSwitch from '@/pages/home/components/ModeSwitch.vue';

// `useColorMode` is a Nuxt auto-import, not available in a bare happy-dom
// environment. It is pre-stubbed in tests/integration/setup.ts; each test can
// override the returned ref via vi.mocked(getColorMode) to observe writes.
type ColorModeApi = { preference: string };

let colorModeApi: ColorModeApi;

beforeEach(() => {
  colorModeApi = { preference: 'light' };
  vi.stubGlobal('useColorMode', vi.fn(() => colorModeApi));
});

describe('ModeSwitch.vue (integration, backend mocked)', () => {
  it('initializes currentMode from useColorMode preference', () => {
    const wrapper = mount(ModeSwitch, {
      global: { stubs: { ToggleSwitch: { template: '<span class="ts"></span>' } } },
    });
    expect(wrapper.find('i').classes()).toContain('pi-sun');
  });

  it('toggles dark mode when the mobile icon is clicked', async () => {
    const wrapper = mount(ModeSwitch, {
      global: { stubs: { ToggleSwitch: { template: '<span class="ts"></span>' } } },
    });
    await wrapper.find('i').trigger('click');
    // mobile click handler passes currentMode==='light' ? 'dark'
    expect(colorModeApi.preference).toBe('dark');
    expect(wrapper.find('i').classes()).toContain('pi-moon');
    expect(vi.mocked(globalThis.useColorMode)).toHaveBeenCalledTimes(1);
  });

  it('binds the ToggleSwitch v-model to true-value/false-value', async () => {
    const wrapper = mount(ModeSwitch, {
      global: {
        stubs: {
          ToggleSwitch: {
            name: 'ToggleSwitch',
            props: ['modelValue', 'trueValue', 'falseValue'],
            emits: ['valueChange', 'update:modelValue'],
            template: '<button class="ts" @click="$emit(\'valueChange\', modelValue)">TS</button>',
          },
        },
      },
    });
    // ToggleSwitch receives currentMode and the value pair
    const ts = wrapper.findComponent({ name: 'ToggleSwitch' });
    expect(ts.exists()).toBe(true);
    expect(ts.props('falseValue')).toBe('light');
    expect(ts.props('trueValue')).toBe('dark');
    expect(ts.props('modelValue')).toBe('light');
  });
});
