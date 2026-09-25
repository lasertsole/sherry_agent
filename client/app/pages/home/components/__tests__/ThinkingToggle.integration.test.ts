import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount } from '@vue/test-utils';
import ThinkingToggle from '@/pages/home/components/ThinkingToggle.vue';

// The component talks to the per-session thinking store (Nuxt auto-import).
// Shadow it the same way ModeSwitch tests shadow useUiStore, so tests can
// drive `mode`/`current` and observe hydrate/setValue calls.
let storeApi: {
  mode: 'on_off' | 'levels';
  current: ReturnType<typeof vi.fn>;
  hydrate: ReturnType<typeof vi.fn>;
  setValue: ReturnType<typeof vi.fn>;
};

beforeEach(() => {
  storeApi = {
    mode: 'on_off',
    current: vi.fn(() => false),
    hydrate: vi.fn(),
    setValue: vi.fn()
  };
  vi.stubGlobal(
    'useThinkingStore',
    vi.fn(() => storeApi)
  );
});

const stubs = {
  ToggleSwitch: { template: '<span class="ts"></span>' },
  Button: {
    props: ['label', 'variant'],
    emits: ['click'],
    template: '<button class="lvl" @click="$emit(\'click\')">{{ label }}</button>'
  }
};

describe('ThinkingToggle.vue (integration, store mocked)', () => {
  it('hydrates the session on mount and renders the switch at the row’s right edge', () => {
    const wrapper = mount(ThinkingToggle, {
      props: { sessionId: 'sid-1' },
      global: { stubs }
    });
    expect(storeApi.hydrate).toHaveBeenCalledWith('sid-1');
    expect(wrapper.find('.ml-auto').exists()).toBe(true);
    expect(wrapper.text()).toContain('思考');
    // on_off mode renders the ToggleSwitch stub, not the level selector
    expect(wrapper.find('.ts').exists()).toBe(true);
    expect(wrapper.findAll('button.lvl').length).toBe(0);
  });

  it('renders the 低/高/最高 selector for always-think models', async () => {
    storeApi.mode = 'levels';
    storeApi.current.mockReturnValue('high');
    const wrapper = mount(ThinkingToggle, {
      props: { sessionId: 'sid-1' },
      global: { stubs }
    });
    const labels = wrapper.findAll('button.lvl').map(b => b.text());
    expect(labels).toEqual(['低', '高', '最高']);
    // the active level renders with the primary variant
    const active = wrapper.findAll('button.lvl').find(b => b.text() === '高');
    expect(active?.attributes('variant')).toBeUndefined(); // prop, not attr
    expect(wrapper.props('sessionId')).toBe('sid-1');
  });

  it('pushes level selections through store.setValue', async () => {
    storeApi.mode = 'levels';
    storeApi.current.mockReturnValue('high');
    const wrapper = mount(ThinkingToggle, {
      props: { sessionId: 'sid-1' },
      global: { stubs }
    });
    const low = wrapper.findAll('button.lvl').find(b => b.text() === '低');
    await low!.trigger('click');
    expect(storeApi.setValue).toHaveBeenCalledWith('sid-1', 'low');
  });

  it('blocks changes while the session is streaming', async () => {
    storeApi.mode = 'levels';
    storeApi.current.mockReturnValue('high');
    const wrapper = mount(ThinkingToggle, {
      props: { sessionId: 'sid-1', streaming: true },
      global: { stubs }
    });
    expect(wrapper.find('.pointer-events-none').exists()).toBe(true);
    const low = wrapper.findAll('button.lvl').find(b => b.text() === '低');
    await low!.trigger('click');
    expect(storeApi.setValue).not.toHaveBeenCalled();
  });

  it('mobile button cycles levels for always-think models', async () => {
    storeApi.mode = 'levels';
    storeApi.current.mockReturnValue('high');
    const wrapper = mount(ThinkingToggle, {
      props: { sessionId: 'sid-1' },
      global: { stubs }
    });
    await wrapper.find('button.block').trigger('click');
    expect(storeApi.setValue).toHaveBeenCalledWith('sid-1', 'max');
  });
});
