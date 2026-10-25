/**
 * TokenBudgetPanel: the per-session cap/reset controls inside System Config.
 *
 * The bridge is mocked; the test asserts the panel loads the current session's
 * state, saves a cap and resets the accumulated usage.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import TokenBudgetPanel from '@/pages/home/components/TokenBudgetPanel.vue';

const budgetApi = vi.hoisted(() => ({
  fetchTokenBudget: vi.fn(),
  setTokenBudgetCap: vi.fn(),
  resetTokenBudgetUsage: vi.fn()
}));

vi.mock('~/composables/bridge/session', () => budgetApi);

const primevueStubs = {
  Button: {
    name: 'Button',
    props: ['label', 'disabled'],
    emits: ['click'],
    template: '<button :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>'
  },
  InputNumber: {
    name: 'InputNumber',
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<input class="in" :value="modelValue" />'
  },
  ToggleSwitch: {
    name: 'ToggleSwitch',
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<input type="checkbox" :checked="modelValue" />'
  },
  Tag: { name: 'Tag', props: ['value'], template: '<span class="tag">{{ value }}</span>' },
  ProgressBar: { name: 'ProgressBar', props: ['value'], template: '<div class="pb"></div>' }
};

interface PanelVm {
  unlimited: boolean;
  capInput: number | null;
  saveCap: () => Promise<void>;
  resetUsage: () => Promise<void>;
}

const state = {
  cap: 1_000_000,
  used: 250_000,
  cost: 0.1234,
  limited: false,
  pending: false
};

async function mountPanel() {
  const wrapper = mount(TokenBudgetPanel, {
    props: { sessionId: 's1' },
    global: { stubs: primevueStubs }
  });
  await flushPromises();
  return wrapper;
}

beforeEach(() => {
  budgetApi.fetchTokenBudget.mockReset().mockResolvedValue({ ...state });
  budgetApi.setTokenBudgetCap.mockReset().mockResolvedValue({ pending: false });
  budgetApi.resetTokenBudgetUsage.mockReset().mockResolvedValue({ ...state, used: 0, cost: 0 });
});

describe('TokenBudgetPanel', () => {
  it('is empty until a session is open', async () => {
    const wrapper = mount(TokenBudgetPanel, {
      props: {},
      global: { stubs: primevueStubs }
    });
    await flushPromises();

    expect(budgetApi.fetchTokenBudget).not.toHaveBeenCalled();
    expect(wrapper.text()).toContain('请先打开一个会话');
  });

  it('shows the loaded cap and usage', async () => {
    const wrapper = await mountPanel();

    expect(budgetApi.fetchTokenBudget).toHaveBeenCalledWith('s1');
    expect(wrapper.text()).toContain('250,000');
    expect(wrapper.text()).toContain('1,000,000');
    expect(wrapper.text()).toContain('$0.1234');
  });

  it('saves a custom cap and resets the accumulated usage', async () => {
    const wrapper = await mountPanel();
    const vm = wrapper.vm as unknown as PanelVm;
    vm.unlimited = false;
    vm.capInput = 500_000;

    await vm.saveCap();
    expect(budgetApi.setTokenBudgetCap).toHaveBeenCalledWith('s1', 500_000);

    await vm.resetUsage();
    expect(budgetApi.resetTokenBudgetUsage).toHaveBeenCalledWith('s1');
    expect(wrapper.text()).toContain('0 / ');
  });

  it('clears the cap when unlimited is selected', async () => {
    const wrapper = await mountPanel();
    const vm = wrapper.vm as unknown as PanelVm;
    vm.unlimited = true;

    await vm.saveCap();
    expect(budgetApi.setTokenBudgetCap).toHaveBeenCalledWith('s1', null);
  });
});
