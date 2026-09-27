import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount } from '@vue/test-utils';
import SessionModelPicker from '@/pages/home/components/SessionModelPicker.vue';

// Both stores are Nuxt auto-imports (like ThinkingToggle's store), so tests
// shadow them as globals and drive the observable surface directly.
let modelApi: {
  currentId: ReturnType<typeof vi.fn>;
  hydrate: ReturnType<typeof vi.fn>;
  select: ReturnType<typeof vi.fn>;
  isPending: ReturnType<typeof vi.fn>;
  envModel: { provider: string | null; model: string | null };
  overrideBySession: Record<string, { id: string; label: string; model: string } | null>;
};
let profilesApi: { listFor: ReturnType<typeof vi.fn> };
// The thinking control's MODE follows the chosen model, so the picker
// re-hydrates it after a switch (stubbed like the other auto-imported stores).
let thinkingApi: { hydrate: ReturnType<typeof vi.fn> };

const profile = {
  id: 'p1',
  label: 'Kimi K2',
  params: {
    MAIN_LLM_PROVIDER: 'openai',
    MAIN_LLM_NAME: 'kimi-k2',
    MAIN_LLM_API_BASE: 'https://api.moonshot.cn/v1',
    MAIN_LLM_API_KEY: 'sk-kimi'
  }
};

beforeEach(() => {
  modelApi = {
    currentId: vi.fn(() => 'env'),
    hydrate: vi.fn(),
    select: vi.fn(),
    isPending: vi.fn(() => false),
    envModel: { provider: 'zhipu', model: 'glm-4.6' },
    overrideBySession: {}
  };
  profilesApi = { listFor: vi.fn(() => [profile]) };
  thinkingApi = { hydrate: vi.fn() };
  vi.stubGlobal(
    'useSessionModelStore',
    vi.fn(() => modelApi)
  );
  vi.stubGlobal(
    'useLlmProfilesStore',
    vi.fn(() => profilesApi)
  );
  vi.stubGlobal(
    'useThinkingStore',
    vi.fn(() => thinkingApi)
  );
});

// Menu popup stub: renders its model items only while "opened"; toggle() flips
// it — mirroring PrimeVue Menu's popup surface (same stub as ThinkingToggle's).
const MenuStub = {
  name: 'Menu',
  props: ['model', 'popup'],
  data: () => ({ opened: false }),
  methods: {
    toggle() {
      this.opened = !this.opened;
    }
  },
  template: `<div v-if="opened" class="menu-stub"><slot name="start" /><button v-for="i in model" :key="i.label" class="opt" @click="i.command()"><i v-if="i.icon" :class="i.icon"></i>{{ i.label }}</button></div>`
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

describe('SessionModelPicker.vue (integration, stores mocked)', () => {
  it('hydrates the session on mount and shows the env model by default', () => {
    const wrapper = mount(SessionModelPicker, {
      props: { sessionId: 'sid-1' },
      global: { stubs }
    });

    expect(modelApi.hydrate).toHaveBeenCalledWith('sid-1');
    // Collapsed: only the trigger, no option list and no header in the row.
    expect(wrapper.findAll('button.opt').length).toBe(0);
    expect(wrapper.find('button.trigger').text()).toContain('glm-4.6');
    expect(wrapper.text()).not.toContain('主模型');
  });

  it('names the control in the list header instead of the toolbar row', async () => {
    const wrapper = mount(SessionModelPicker, {
      props: { sessionId: 'sid-1' },
      global: { stubs }
    });

    await wrapper.find('button.trigger').trigger('click');

    const menu = wrapper.find('.menu-stub');
    // Header first, options after it.
    expect(menu.element.firstElementChild?.textContent).toContain('主模型');
    expect(menu.findAll('button.opt').length).toBe(2);
    expect(wrapper.find('.model-trigger').text()).not.toContain('主模型');
  });

  it('offers the env entry plus the MAIN_LLM profiles, marking the current one', async () => {
    const wrapper = mount(SessionModelPicker, {
      props: { sessionId: 'sid-1' },
      global: { stubs }
    });

    await wrapper.find('button.trigger').trigger('click');

    const labels = wrapper.findAll('button.opt').map(b => b.text());
    expect(labels[0]).toContain('glm-4.6');
    expect(labels[1]).toBe('kimi-k2');
    const checked = wrapper.findAll('button.opt').find(b => b.text().includes('glm-4.6'));
    expect(checked?.find('i').classes()).toContain('pi-check');
    expect(profilesApi.listFor).toHaveBeenCalledWith('MAIN_LLM');
  });

  it('pushes a profile selection through store.select with the descriptor', async () => {
    const wrapper = mount(SessionModelPicker, {
      props: { sessionId: 'sid-1' },
      global: { stubs }
    });

    await wrapper.find('button.trigger').trigger('click');
    const option = wrapper.findAll('button.opt').find(b => b.text() === 'kimi-k2');
    await option!.trigger('click');

    expect(modelApi.select).toHaveBeenCalledWith('sid-1', {
      id: 'p1',
      label: 'Kimi K2',
      model: 'kimi-k2',
      provider: 'openai',
      base_url: 'https://api.moonshot.cn/v1',
      api_key: 'sk-kimi'
    });
    // The thinking control's mode follows the new model.
    expect(thinkingApi.hydrate).toHaveBeenCalledWith('sid-1');
  });

  it('sends null when 跟随环境配置 is chosen', async () => {
    modelApi.currentId.mockReturnValue('p1');
    modelApi.overrideBySession = { 'sid-1': { id: 'p1', label: 'Kimi K2', model: 'kimi-k2' } };
    const wrapper = mount(SessionModelPicker, {
      props: { sessionId: 'sid-1' },
      global: { stubs }
    });
    expect(wrapper.find('button.trigger').text()).toBe('kimi-k2');

    await wrapper.find('button.trigger').trigger('click');
    const envEntry = wrapper.findAll('button.opt')[0]!;
    await envEntry.trigger('click');

    expect(modelApi.select).toHaveBeenCalledWith('sid-1', null);
  });

  it('skips profiles without a model name', async () => {
    profilesApi.listFor.mockReturnValue([
      profile,
      { id: 'broken', label: 'Broken', params: { MAIN_LLM_PROVIDER: 'openai' } }
    ]);
    const wrapper = mount(SessionModelPicker, {
      props: { sessionId: 'sid-1' },
      global: { stubs }
    });

    await wrapper.find('button.trigger').trigger('click');

    expect(wrapper.findAll('button.opt').map(b => b.text())).toEqual(['跟随环境配置（glm-4.6）', 'kimi-k2']);
  });

  it('allows switching while a turn is running (parked for the next turn)', async () => {
    modelApi.isPending.mockReturnValue(true);
    const wrapper = mount(SessionModelPicker, {
      props: { sessionId: 'sid-1' },
      global: { stubs }
    });

    // No streaming lock; the parked choice is announced with a clock hint.
    expect(wrapper.find('.pointer-events-none').exists()).toBe(false);
    expect(wrapper.find('i.pi-clock').exists()).toBe(true);
    await wrapper.find('button.trigger').trigger('click');
    const option = wrapper.findAll('button.opt').find(b => b.text() === 'kimi-k2');
    await option!.trigger('click');
    expect(modelApi.select).toHaveBeenCalled();
  });

  it('hides the clock hint when nothing is parked', () => {
    const wrapper = mount(SessionModelPicker, {
      props: { sessionId: 'sid-1' },
      global: { stubs }
    });
    expect(wrapper.find('i.pi-clock').exists()).toBe(false);
  });

  it('truncates long model names in the collapsed trigger', () => {
    modelApi.overrideBySession = {
      'sid-1': { id: 'p1', label: 'long', model: 'a-very-long-model-name-here' }
    };
    modelApi.currentId.mockReturnValue('p1');
    const wrapper = mount(SessionModelPicker, {
      props: { sessionId: 'sid-1' },
      global: { stubs }
    });

    const text = wrapper.find('button.trigger').text();
    expect(text.length).toBeLessThanOrEqual(19);
    expect(text.endsWith('…')).toBe(true);
  });
});
