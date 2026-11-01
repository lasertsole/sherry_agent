/**
 * TokenBudgetPanel: the per-session cap/reset controls, now a session-scoped
 * right-sidebar tab (it takes the session from the tab payload).
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
  turnUnlimited: boolean;
  turnCapInput: number | null;
  saveCap: () => Promise<void>;
  resetUsage: () => Promise<void>;
}

const state = {
  cap: 1_000_000,
  session_cap: 1_000_000,
  default_cap: 0,
  inherited: false,
  used: 250_000,
  cost: 0.1234,
  limited: false,
  pending: false,
  turn_cap: null,
  session_turn_cap: 0,
  default_turn_cap: 0,
  inherited_turn: false,
  turn_used: 120_000,
  turn_main_used: 100_000,
  turn_child_used: 20_000,
  turn_cost: 0.05,
  turn_limited: false,
  turn_pending: false,
  turn_id: 't1'
};

async function mountPanel() {
  const wrapper = mount(TokenBudgetPanel, {
    props: { payload: { sessionId: 's1' } },
    global: { stubs: primevueStubs }
  });
  await flushPromises();
  return wrapper;
}

beforeEach(() => {
  budgetApi.fetchTokenBudget.mockReset().mockResolvedValue({ ...state });
  budgetApi.setTokenBudgetCap.mockReset().mockResolvedValue({ pending: false, turn_pending: false });
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
    expect(wrapper.text()).toContain('120,000');
    expect(wrapper.text()).toContain('1,000,000');
    expect(wrapper.text()).toContain('$0.1234');
  });

  it('saves a custom cap and resets the accumulated usage', async () => {
    const wrapper = await mountPanel();
    const vm = wrapper.vm as unknown as PanelVm;
    vm.unlimited = false;
    vm.capInput = 500_000;

    await vm.saveCap();
    expect(budgetApi.setTokenBudgetCap).toHaveBeenCalledWith('s1', 500_000, 0);

    await vm.resetUsage();
    expect(budgetApi.resetTokenBudgetUsage).toHaveBeenCalledWith('s1');
    expect(wrapper.text()).toContain('0 / ');
  });

  it('writes an explicit 0 (unlimited) when the toggle selects it', async () => {
    const wrapper = await mountPanel();
    const vm = wrapper.vm as unknown as PanelVm;
    vm.unlimited = true;

    await vm.saveCap();
    expect(budgetApi.setTokenBudgetCap).toHaveBeenCalledWith('s1', 0, 0);
  });

  it('treats an emptied cap field as unlimited', async () => {
    const wrapper = await mountPanel();
    const vm = wrapper.vm as unknown as PanelVm;
    vm.unlimited = false;
    vm.capInput = null;
    // The save reloads the state: the backend now reports the explicit 0.
    budgetApi.fetchTokenBudget.mockResolvedValue({
      ...state,
      cap: null,
      session_cap: 0,
      inherited: false,
      used: 0,
      cost: 0
    });

    await vm.saveCap();

    expect(budgetApi.setTokenBudgetCap).toHaveBeenCalledWith('s1', 0, 0);
    expect(vm.unlimited).toBe(true);
  });

  it('says when the cap is inherited from the app config', async () => {
    budgetApi.fetchTokenBudget.mockResolvedValue({
      ...state,
      cap: 900_000,
      session_cap: null,
      default_cap: 900_000,
      inherited: true,
      used: 1000,
      cost: 0
    });
    const wrapper = await mountPanel();

    // The inherited cap is what applies, and the hint names its source.
    expect(wrapper.text()).toContain('900,000');
    expect(wrapper.find('[data-test="budget-source"]').text()).toContain('沿用');
    expect(wrapper.find('[data-test="budget-source"]').text()).toContain('900,000');
  });

  it('saves a per-turn cap together with the session cap', async () => {
    const wrapper = await mountPanel();
    const vm = wrapper.vm as unknown as PanelVm;
    vm.turnUnlimited = false;
    vm.turnCapInput = 80_000;

    await vm.saveCap();

    expect(budgetApi.setTokenBudgetCap).toHaveBeenCalledWith('s1', 1_000_000, 80_000);
  });

  it('says when the session carries its own choice', async () => {
    const wrapper = await mountPanel();

    expect(wrapper.find('[data-test="budget-source"]').text()).toContain('本会话单独设置');
  });

  it('shows an inherited UNLIMITED default as unlimited', async () => {
    budgetApi.fetchTokenBudget.mockResolvedValue({
      ...state,
      cap: null,
      session_cap: null,
      default_cap: 0,
      inherited: true,
      used: 0,
      cost: 0
    });
    const wrapper = await mountPanel();

    expect(wrapper.find('[data-test="budget-source"]').text()).toContain('不限制');
  });
});
