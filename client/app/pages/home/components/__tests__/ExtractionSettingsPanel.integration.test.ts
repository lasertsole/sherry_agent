/**
 * The 预设角色 → 高级 tab body.
 *
 * Both scopes render the same four rows; the contract is where they differ:
 * flow replay compilation is read-only in a session (the lock hint is visible
 * and the switch is disabled), a session save sends ONLY the fields that
 * differ from the global values (untouched fields keep inheriting), the global
 * save sends all four with flow forced off when experience learning is off,
 * and the reset action clears every override.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import ExtractionSettingsPanel from '@/pages/home/components/ExtractionSettingsPanel.vue';

const bridge = vi.hoisted(() => ({
  fetchExtractionState: vi.fn(),
  fetchGlobalExtraction: vi.fn(),
  saveGlobalExtraction: vi.fn(),
  saveSessionExtraction: vi.fn()
}));
vi.mock('~/composables/bridge/extraction', () => bridge);

const GLOBAL = {
  skill_extraction_enabled: true,
  flow_extraction_enabled: true,
  iteration_budget_main: 180,
  iteration_budget_sub: 90
};

/**
 * Session state with optional overrides and an effective merge.
 * @param overrides
 * @param pending
 */
function sessionState(overrides: Record<string, unknown> = {}, pending = false) {
  const effective = { ...GLOBAL, ...overrides };
  if (!effective.skill_extraction_enabled) effective.flow_extraction_enabled = false;
  return {
    global: GLOBAL,
    session: overrides,
    effective,
    pending,
    defaults: GLOBAL,
    bounds: { min_iterations: 1, max_iterations: 1000 }
  };
}

const stubs = {
  ProgressSpinner: { name: 'ProgressSpinner', template: '<div class="spin" />' },
  ToggleSwitch: {
    name: 'ToggleSwitch',
    props: ['modelValue', 'disabled'],
    emits: ['update:modelValue'],
    template: `<input
      type="checkbox"
      :checked="modelValue"
      :disabled="disabled"
      @change="$emit('update:modelValue', $event.target.checked)" />`
  },
  Button: {
    name: 'Button',
    props: ['label', 'disabled', 'loading'],
    emits: ['click'],
    template: `<button :disabled="disabled" @click="$emit('click')">{{ label }}</button>`
  }
};

/**
 * Mount one scope and let the on-mount load settle.
 * @param scope
 * @param sessionId
 */
async function mountPanel(scope: 'global' | 'session', sessionId = 'sid-1') {
  const wrapper = mount(ExtractionSettingsPanel, {
    props: { scope, sessionId },
    global: { stubs }
  });
  await flushPromises();
  return wrapper;
}

describe('ExtractionSettingsPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    bridge.fetchExtractionState.mockResolvedValue(sessionState());
    bridge.fetchGlobalExtraction.mockResolvedValue({
      global: GLOBAL,
      defaults: GLOBAL,
      bounds: { min_iterations: 1, max_iterations: 1000 }
    });
    bridge.saveSessionExtraction.mockImplementation(async (_sid: string, overrides: Record<string, unknown>) => {
      const state = sessionState(overrides);
      return { overrides, effective: state.effective, pending: false };
    });
    bridge.saveGlobalExtraction.mockImplementation(async (values: Record<string, unknown>) => ({
      ...GLOBAL,
      ...values
    }));
  });

  it('shows the flow switch read-only in a session', async () => {
    const wrapper = await mountPanel('session');

    const flow = wrapper.get('[data-test="extraction-flow-switch"]');
    expect((flow.element as HTMLInputElement).disabled).toBe(true);
    expect(wrapper.find('[data-test="extraction-flow-global-only"]').exists()).toBe(true);
    // Experience learning stays editable.
    const skill = wrapper.get('[data-test="extraction-skill-switch"]');
    expect((skill.element as HTMLInputElement).disabled).toBe(false);
  });

  it('saves only the fields that differ from the global values', async () => {
    const wrapper = await mountPanel('session');

    await wrapper.get('[data-test="extraction-skill-switch"]').setValue(false);
    await wrapper.get('[data-test="extraction-save"]').trigger('click');
    await flushPromises();

    expect(bridge.saveSessionExtraction).toHaveBeenCalledWith('sid-1', {
      skill_extraction_enabled: false
    });
  });

  it('marks inherited vs overridden fields', async () => {
    bridge.fetchExtractionState.mockResolvedValue(sessionState({ iteration_budget_main: 120 }, true));
    const wrapper = await mountPanel('session');

    const text = wrapper.text();
    expect(text).toContain('本会话已覆盖');
    // The untouched child ceiling still shows the value it inherits.
    expect(text).toContain('继承自全局（90）');
  });

  it('rejects a child ceiling above the main one', async () => {
    const wrapper = await mountPanel('session');

    await wrapper.get('[data-test="extraction-main-input"]').setValue(10);
    await wrapper.get('[data-test="extraction-sub-input"]').setValue(50);

    expect(wrapper.find('[data-test="extraction-validation-error"]').text()).toContain('子智能体上限不应高于主智能体');
    expect((wrapper.get('[data-test="extraction-save"]').element as HTMLButtonElement).disabled).toBe(true);
    expect(bridge.saveSessionExtraction).not.toHaveBeenCalled();
  });

  it('resets every session override', async () => {
    bridge.fetchExtractionState.mockResolvedValue(sessionState({ skill_extraction_enabled: false }, true));
    const wrapper = await mountPanel('session');

    await wrapper.get('[data-test="extraction-reset"]').trigger('click');
    await flushPromises();

    expect(bridge.saveSessionExtraction).toHaveBeenCalledWith('sid-1', {});
  });

  it('writes all four global values and forces flow off when skill is off', async () => {
    const wrapper = await mountPanel('global');

    await wrapper.get('[data-test="extraction-skill-switch"]').setValue(false);
    await wrapper.get('[data-test="extraction-save"]').trigger('click');
    await flushPromises();

    expect(bridge.saveGlobalExtraction).toHaveBeenCalledWith({
      skill_extraction_enabled: false,
      flow_extraction_enabled: false,
      iteration_budget_main: 180,
      iteration_budget_sub: 90
    });
  });

  it('disables the flow switch in the global scope while skill is off', async () => {
    const wrapper = await mountPanel('global');

    await wrapper.get('[data-test="extraction-skill-switch"]').setValue(false);

    const flow = wrapper.get('[data-test="extraction-flow-switch"]');
    expect((flow.element as HTMLInputElement).disabled).toBe(true);
    expect(wrapper.find('[data-test="extraction-flow-requires-skill"]').exists()).toBe(true);
  });
});
