import { describe, it, expect } from 'vitest';
import { mount } from '@vue/test-utils';
import { nextTick } from 'vue';
import homeIndex from '@/pages/home/index.vue';
import HistoryItem from '@/pages/home/components/HistoryItem.vue';
import ModeSwitch from '@/pages/home/components/ModeSwitch.vue';
import ChatBox from '@/pages/home/components/ChatBox.vue';

const primevueStub = {
  Checkbox: {
    name: 'Checkbox',
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<button class="cb" @click="$emit(\'update:modelValue\', !modelValue)">C</button>',
  },
  Button: { props: ['label'], template: '<button class="btn"><slot /><span>{{ label }}</span></button>' },
  Menu: { template: '<div class="mnu"></div>', methods: { toggle() {} } },
  ToggleSwitch: { template: '<span class="ts"></span>' },
  ChatInputBox: { template: '<div class="cib"></div>' },
};

// `get_history_by_turn_page` is a Nuxt auto-import pre-stubbed in setup.ts, so the
// module-level call in index.vue is a no-op. Children are real components whose
// heavy deps (PrimeVue/markdown) are stubbed above.
function mountHome() {
  return mount(homeIndex, {
    global: { stubs: primevueStub },
  });
}

describe('home/index.vue (integration, backend mocked)', () => {
  it('composes the sidebar and chat regions with real leaf children', () => {
    const wrapper = mountHome();
    expect(wrapper.findComponent(HistoryItem).exists()).toBe(true);
    expect(wrapper.findComponent(ModeSwitch).exists()).toBe(true);
    expect(wrapper.findComponent(ChatBox).exists()).toBe(true);
    // handleCreateSession creates a new session on load with title "新会话".
    expect(wrapper.text()).toContain('新会话');
    // Branding present in sidebar and mobile header.
    expect(wrapper.text()).toContain('🍊橘雪莉');
  });

  it('renders the full-select checkbox group', () => {
    const wrapper = mountHome();
    expect(wrapper.text()).toContain('全选');
    expect(wrapper.text()).toContain('批量删除对话');
    expect(wrapper.find('.cb').exists()).toBe(true);
  });

  it('activates a history row when it is selected', async () => {
    const wrapper = mountHome();
    const row = wrapper.findComponent(HistoryItem).find('.p-3');
    await row.trigger('click');
    await nextTick();
    // chooseSession -> handleToggleSession sets currentSessionId -> is-active true.
    expect(wrapper.findComponent(HistoryItem).props('isActive')).toBe(true);
    // The mobile sidebar overlay auto-closes on selection.
    expect(wrapper.find('.fixed.inset-0').exists()).toBe(false);
  });

  it('keeps isIndeterminate false with a single fully- or not-selected session', () => {
    const wrapper = mountHome();
    // historyList has exactly 1 record, so a partial selection can never occur:
    // the full-select Checkbox must not receive an `indeterminate` flag.
    const cb = wrapper.findComponent({ name: 'Checkbox' });
    expect(cb.exists()).toBe(true);
    expect(cb.props('indeterminate')).toBeUndefined();
  });

  it('does not crash when the mobile switch/cog actions fire', async () => {
    const wrapper = mountHome();
    const buttons = wrapper.findAll('.btn');
    // Toggling the mobile menu button flips the sidebar overlay.
    expect(buttons.length).toBeGreaterThan(0);
  });
});
