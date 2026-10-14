import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import ThinkingCustomMappingPanel from '@/pages/home/components/ThinkingCustomMappingPanel.vue';

// The panel talks to the request bridge; shadow it so tests drive the payloads
// (the same pattern the other settings panels use).
const bridge = vi.hoisted(() => ({
  fetchCustomThinkingMapping: vi.fn(),
  setCustomThinkingMapping: vi.fn()
}));

vi.mock('~/composables/bridge/session', () => bridge);

const stubs = {
  InputText: {
    props: ['modelValue', 'disabled', 'placeholder'],
    emits: ['update:modelValue'],
    template:
      '<input class="ipt" :value="modelValue" :disabled="disabled" @input="$emit(\'update:modelValue\', $event.target.value)" />'
  },
  Select: {
    props: ['modelValue', 'options'],
    emits: ['update:modelValue'],
    template: '<select class="sel" :value="modelValue"></select>'
  },
  Button: {
    props: ['label', 'disabled', 'loading', 'severity', 'variant'],
    emits: ['click'],
    template: '<button class="btn" :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>'
  }
};

const i18n = {
  global: { stubs },
  mocks: { t: (key: string) => key }
};

describe('ThinkingCustomMappingPanel (integration, bridge mocked)', () => {
  beforeEach(() => {
    bridge.fetchCustomThinkingMapping.mockReset();
    bridge.setCustomThinkingMapping.mockReset();
    bridge.fetchCustomThinkingMapping.mockResolvedValue({ mapping: null, pending: false });
  });

  it('stays collapsed and hydrates without a saved mapping', async () => {
    const wrapper = mount(ThinkingCustomMappingPanel, {
      props: { sessionId: 'sid-1' },
      ...i18n
    });
    await flushPromises();

    expect(bridge.fetchCustomThinkingMapping).toHaveBeenCalledWith('sid-1');
    expect(wrapper.find('[data-test="thinking-custom-hint"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="thinking-custom-active"]').exists()).toBe(false);
  });

  it('opens and shows the pill when a mapping is saved', async () => {
    bridge.fetchCustomThinkingMapping.mockResolvedValueOnce({
      mapping: {
        wire_field: 'extra_body.custom.think',
        wire_format: 'custom',
        level_map: { off: 'none', high: 'full' }
      },
      pending: false
    });
    const wrapper = mount(ThinkingCustomMappingPanel, {
      props: { sessionId: 'sid-2' },
      ...i18n
    });
    await flushPromises();

    expect(wrapper.find('[data-test="thinking-custom-active"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="thinking-custom-hint"]').exists()).toBe(true);
    const field = wrapper.find('[data-test="thinking-custom-wire-field"]').element as HTMLInputElement;
    expect(field.value).toBe('extra_body.custom.think');
    const high = wrapper.find('[data-test="thinking-custom-level-high"]').element as HTMLInputElement;
    expect(high.value).toBe('full');
  });

  it('saves the draft through the bridge', async () => {
    bridge.setCustomThinkingMapping.mockResolvedValueOnce({ pending: false });
    const wrapper = mount(ThinkingCustomMappingPanel, {
      props: { sessionId: 'sid-3' },
      ...i18n
    });
    await flushPromises();
    await wrapper.find('[data-test="thinking-custom-toggle"]').trigger('click');

    const field = wrapper.find('[data-test="thinking-custom-wire-field"]');
    await field.setValue('extra_body.a.b');
    const high = wrapper.find('[data-test="thinking-custom-level-high"]');
    await high.setValue('full');
    await wrapper.find('[data-test="thinking-custom-save"]').trigger('click');
    await flushPromises();

    expect(bridge.setCustomThinkingMapping).toHaveBeenCalledWith('sid-3', {
      wire_field: 'extra_body.a.b',
      wire_format: 'custom',
      level_map: { high: 'full' }
    });
    expect(wrapper.find('[data-test="thinking-custom-saved"]').exists()).toBe(true);
  });

  it('marks a level unsupported via the checkbox (null on the wire)', async () => {
    bridge.setCustomThinkingMapping.mockResolvedValueOnce({ pending: false });
    const wrapper = mount(ThinkingCustomMappingPanel, {
      props: { sessionId: 'sid-4' },
      ...i18n
    });
    await flushPromises();
    await wrapper.find('[data-test="thinking-custom-toggle"]').trigger('click');
    await wrapper.find('[data-test="thinking-custom-level-high"]').setValue('full');
    await wrapper.find('[data-test="thinking-custom-unsupported-high"]').trigger('change');
    await wrapper.find('[data-test="thinking-custom-save"]').trigger('click');
    await flushPromises();

    expect(bridge.setCustomThinkingMapping).toHaveBeenCalledWith('sid-4', {
      wire_field: '',
      wire_format: 'custom',
      level_map: { high: null }
    });
  });

  it('reset clears the mapping and shows the server refusal inline', async () => {
    bridge.setCustomThinkingMapping.mockRejectedValueOnce(new Error('bad wire_field'));
    const wrapper = mount(ThinkingCustomMappingPanel, {
      props: { sessionId: 'sid-5' },
      ...i18n
    });
    await flushPromises();
    await wrapper.find('[data-test="thinking-custom-toggle"]').trigger('click');
    await wrapper.find('[data-test="thinking-custom-save"]').trigger('click');
    await flushPromises();

    expect(wrapper.find('[data-test="thinking-custom-error"]').text()).toContain('bad wire_field');

    bridge.setCustomThinkingMapping.mockResolvedValueOnce({ pending: false });
    await wrapper.find('[data-test="thinking-custom-reset"]').trigger('click');
    await flushPromises();

    expect(bridge.setCustomThinkingMapping).toHaveBeenLastCalledWith('sid-5', null);
    expect(wrapper.find('[data-test="thinking-custom-active"]').exists()).toBe(false);
  });
});
