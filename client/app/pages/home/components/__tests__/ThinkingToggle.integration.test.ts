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

// Menu popup stub: renders its model items only while "opened"; toggle()
// flips it — mirroring PrimeVue Menu's popup surface.
const MenuStub = {
  name: 'Menu',
  props: ['model', 'popup'],
  data: () => ({ opened: false }),
  methods: {
    toggle() {
      this.opened = !this.opened;
    }
  },
  template: `<div v-if="opened" class="menu-stub"><button v-for="i in model" :key="i.label" class="lvl" @click="i.command()"><i v-if="i.icon" :class="i.icon"></i>{{ i.label }}</button></div>`
};

const stubs = {
  Button: {
    props: ['label', 'variant', 'icon', 'iconPos'],
    emits: ['click'],
    template:
      '<button class="trigger" @click="$emit(\'click\', $event)">{{ label }}<i v-if="icon" :class="icon"></i></button>'
  },
  Menu: MenuStub
};

describe('ThinkingToggle.vue (integration, store mocked)', () => {
  it('hydrates the session on mount and renders the collapsed picker in on_off mode', () => {
    const wrapper = mount(ThinkingToggle, {
      props: { sessionId: 'sid-1' },
      global: { stubs }
    });
    expect(storeApi.hydrate).toHaveBeenCalledWith('sid-1');
    expect(wrapper.find('.ml-auto').exists()).toBe(true);
    expect(wrapper.text()).toContain('思考');
    // on_off mode is a picker too (not a switch): the trigger names the current
    // state and the option list stays hidden until it is clicked.
    expect(wrapper.findAll('button.lvl').length).toBe(0);
    expect(wrapper.find('button.trigger').text()).toBe('关闭');
  });

  it('offers 关闭/开启 in on_off mode and marks the current one', async () => {
    storeApi.current.mockReturnValue(true);
    const wrapper = mount(ThinkingToggle, {
      props: { sessionId: 'sid-1' },
      global: { stubs }
    });
    expect(wrapper.find('button.trigger').text()).toBe('开启');
    await wrapper.find('button.trigger').trigger('click');
    expect(wrapper.findAll('button.lvl').map(b => b.text())).toEqual(['开启', '关闭']);
    const checked = wrapper.findAll('button.lvl').find(b => b.text() === '开启');
    expect(checked?.find('i').classes()).toContain('pi-check');
  });

  it('pushes on_off selections through store.setValue', async () => {
    const wrapper = mount(ThinkingToggle, {
      props: { sessionId: 'sid-1' },
      global: { stubs }
    });
    await wrapper.find('button.trigger').trigger('click');
    const on = wrapper.findAll('button.lvl').find(b => b.text() === '开启');
    await on!.trigger('click');
    expect(storeApi.setValue).toHaveBeenCalledWith('sid-1', true);
  });

  it('collapsed picker shows only the current level until clicked', async () => {
    storeApi.mode = 'levels';
    storeApi.current.mockReturnValue('high');
    const wrapper = mount(ThinkingToggle, {
      props: { sessionId: 'sid-1' },
      global: { stubs }
    });
    // collapsed: exactly one trigger showing the current selection, no list
    expect(wrapper.findAll('button.lvl').length).toBe(0);
    expect(wrapper.find('button.trigger').text()).toBe('高');
    // click opens the list
    await wrapper.find('button.trigger').trigger('click');
    const labels = wrapper.findAll('button.lvl').map(b => b.text());
    expect(labels).toEqual(['低', '高', '最高']);
    // the current level carries the check icon
    const activeItem = wrapper.findAll('button.lvl').find(b => b.text() === '高');
    expect(activeItem?.find('i').classes()).toContain('pi-check');
  });

  it('pushes level selections through store.setValue', async () => {
    storeApi.mode = 'levels';
    storeApi.current.mockReturnValue('high');
    const wrapper = mount(ThinkingToggle, {
      props: { sessionId: 'sid-1' },
      global: { stubs }
    });
    await wrapper.find('button.trigger').trigger('click');
    const low = wrapper.findAll('button.lvl').find(b => b.text() === '低');
    await low!.trigger('click');
    expect(storeApi.setValue).toHaveBeenCalledWith('sid-1', 'low');
  });

  it('blocks opening the popup while the session is streaming', async () => {
    storeApi.mode = 'levels';
    storeApi.current.mockReturnValue('high');
    const wrapper = mount(ThinkingToggle, {
      props: { sessionId: 'sid-1', streaming: true },
      global: { stubs }
    });
    expect(wrapper.find('.pointer-events-none').exists()).toBe(true);
    await wrapper.find('button.trigger').trigger('click');
    expect(wrapper.findAll('button.lvl').length).toBe(0);
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
