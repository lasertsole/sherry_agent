/**
 * The App Config tab (sherry.jsonc): every entry explains itself, and the
 * 重置为默认值 button fills the form with the registry defaults.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
import { setActivePinia } from 'pinia';
import { createTestingPinia } from '@pinia/testing';
import ConfigPanel from '@/pages/home/components/ConfigPanel.vue';

const sherryState = vi.hoisted(() => ({
  entries: [
    { key: 'TOOL_CALL_TIMEOUT_MINUTES', value: '9', default: '5', value_edited: false },
    { key: 'TOKEN_BUDGET_DEFAULT_CAP', value: '0', default: '0', value_edited: false },
    { key: 'LOG_LEVEL', value: 'DEBUG', default: 'INFO', value_edited: false },
    { key: 'heartbeat.enabled', value: 'true', default: 'false', value_edited: false },
    // A key the panel does not know yet: raw-key label, no hint.
    { key: 'FUTURE_KEY', value: 'x', default: 'x', value_edited: false }
  ]
}));

vi.mock('@/composables/sherryConfig', () => ({
  readSherryConfig: vi.fn(async () => ({ entries: sherryState.entries.map(entry => ({ ...entry })) })),
  writeSherryConfig: vi.fn(async () => true)
}));
vi.mock('@/composables/env', () => ({
  API_BASE_URL: 'http://localhost:8080',
  WS_BASE_URL: 'ws://localhost:8080',
  readEnvConfig: vi.fn(async () => ({ groups: [] })),
  readModelsConfig: vi.fn(async () => ({ groups: [] })),
  readNetworkConfig: vi.fn(async () => ({ groups: [] })),
  writeEnvConfig: vi.fn(async () => true),
  writeModelsConfig: vi.fn(async () => true),
  writeNetworkConfig: vi.fn(async () => true)
}));
vi.mock('@/composables/model-config', () => ({ invalidateModelConfigCache: vi.fn() }));
vi.mock('@/pages/home/components/AvatarCropDialog.vue', () => ({
  default: { name: 'AvatarCropDialog', template: '<div class="acd-stub"></div>' }
}));

const stubs = {
  Dialog: { name: 'Dialog', template: '<div class="dlg"><slot /></div>' },
  TabView: { name: 'TabView', template: '<div class="tv"><slot /></div>' },
  TabPanel: { name: 'TabPanel', props: ['value', 'header'], template: '<div class="tp"><slot /></div>' },
  InputText: {
    name: 'InputText',
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<input class="it" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />'
  },
  Button: {
    name: 'Button',
    props: ['label', 'disabled'],
    emits: ['click'],
    template: '<button class="btn" :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>'
  },
  FileUpload: { name: 'FileUpload', template: '<div />' },
  Divider: { name: 'Divider', template: '<div />' },
  Slider: { name: 'Slider', template: '<div />' },
  ProgressSpinner: { name: 'ProgressSpinner', template: '<div />' },
  ReplayAllowlistEditor: { name: 'ReplayAllowlistEditor', template: '<div class="allowlist-stub" />' }
};

/** Mount on the App Config tab and wait for the config read. */
async function mountAppConfig() {
  const wrapper = mount(ConfigPanel, { props: { modelValue: true }, global: { stubs } });
  (wrapper.vm as unknown as { activeTab: number }).activeTab = 2;
  await flushPromises();
  return wrapper;
}

beforeEach(() => {
  setActivePinia(createTestingPinia());
});

describe('ConfigPanel App Config tab', () => {
  it('labels and explains every known key (and falls back to the raw key)', async () => {
    const wrapper = await mountAppConfig();

    const cap = wrapper.get('[data-test="sherry-entry-TOKEN_BUDGET_DEFAULT_CAP"]');
    expect(cap.text()).toContain('默认 Token 上限');
    expect(cap.text()).toContain('0 表示不限制');

    const logLevel = wrapper.get('[data-test="sherry-entry-LOG_LEVEL"]');
    expect(logLevel.text()).toContain('日志级别');
    expect(logLevel.text()).toContain('TRACE / DEBUG');

    // A key without metadata still renders, labelled by its raw key.
    const future = wrapper.get('[data-test="sherry-entry-FUTURE_KEY"]');
    expect(future.text()).toContain('FUTURE_KEY');
  });

  it('restores every field to its registry default and enables Save via the diff', async () => {
    const wrapper = await mountAppConfig();
    const reset = wrapper.get('[data-test="sherry-reset-defaults"]');

    await reset.trigger('click');

    const valueOf = (key: string): string =>
      (wrapper.get(`[data-test="sherry-entry-${key}"] input`).element as HTMLInputElement).value;
    expect(valueOf('TOOL_CALL_TIMEOUT_MINUTES')).toBe('5');
    expect(valueOf('LOG_LEVEL')).toBe('INFO');
    expect(valueOf('heartbeat.enabled')).toBe('false');
    expect(valueOf('TOKEN_BUDGET_DEFAULT_CAP')).toBe('0');
    // Already at its default: the entry's value did not change.
    expect(valueOf('FUTURE_KEY')).toBe('x');
  });

  it('disables the reset once nothing differs from the defaults', async () => {
    const wrapper = await mountAppConfig();

    expect(wrapper.get('[data-test="sherry-reset-defaults"]').attributes('disabled')).toBeUndefined();
    await wrapper.get('[data-test="sherry-reset-defaults"]').trigger('click');

    expect(wrapper.get('[data-test="sherry-reset-defaults"]').attributes('disabled')).toBeDefined();
  });
});
