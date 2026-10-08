/**
 * The env panel renders a group by its declared KIND (backend-mocked).
 *
 * `GET /env` tags every group: model groups (MAIN_LLM, EMBEDDING, …) carry the
 * provider/model/api_key profile manager, everything else is a plain key/value
 * card. The browser feature's switches (`SHERRY_BROWSER_*`) are the first plain
 * group that is NOT the catch-all `other`, so the renderer must key off `kind`
 * — not off "the name is not other".
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { setActivePinia } from 'pinia';
import { createTestingPinia } from '@pinia/testing';
import type { EnvConfigPayload, EnvGroup } from '@/composables/env';
import ConfigPanel from '@/pages/home/components/ConfigPanel.vue';

const envState = vi.hoisted(() => {
  const groups: EnvGroup[] = [
    {
      name: 'SHERRY_BROWSER',
      kind: 'plain',
      entries: [
        { key: 'SHERRY_BROWSER_AGENT_ENABLED', value: '1', value_edited: false },
        { key: 'SHERRY_BROWSER_HEADLESS', value: '1', value_edited: false }
      ]
    },
    {
      name: 'MAIN_LLM',
      kind: 'model',
      entries: [{ key: 'MAIN_LLM_NAME', value: 'deepseek-flash', value_edited: false }]
    }
  ];
  return { groups };
});

vi.mock('@/composables/env', () => ({
  readEnvConfig: vi.fn(async (): Promise<EnvConfigPayload> => ({ groups: envState.groups })),
  writeEnvConfig: vi.fn(async () => true)
}));
vi.mock('@/composables/model-config', () => ({ invalidateModelConfigCache: vi.fn() }));
vi.mock('@/pages/home/components/AvatarCropDialog.vue', () => ({
  default: { name: 'AvatarCropDialog', template: '<div class="acd-stub"></div>' }
}));

const primevueStubs = {
  Dialog: {
    name: 'Dialog',
    props: ['visible', 'header', 'modal', 'closable'],
    emits: ['update:visible', 'show', 'hide'],
    template: '<div class="dlg"><slot /><slot name="footer" /></div>'
  },
  TabView: { name: 'TabView', props: ['activeIndex'], template: '<div class="tv"><slot /></div>' },
  TabPanel: { name: 'TabPanel', props: ['value', 'header'], template: '<div class="tp"><slot /></div>' },
  InputText: {
    name: 'InputText',
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<input class="it" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />'
  },
  FileUpload: { name: 'FileUpload', template: '<div class="fu"><slot name="filelabel" :files="[]" /></div>' },
  Divider: { name: 'Divider', template: '<div class="dv"></div>' },
  Button: { name: 'Button', props: ['label'], template: '<button class="btn">{{ label }}</button>' },
  Slider: { name: 'Slider', props: ['modelValue'], template: '<div class="sl"></div>' },
  ProgressSpinner: { name: 'ProgressSpinner', template: '<div class="ps"></div>' }
};

interface ConfigPanelVm {
  activeTab: number;
  envGroups: EnvGroup[];
}

async function mountEnvTab() {
  const wrapper = mount(ConfigPanel, {
    props: { modelValue: true },
    global: { stubs: primevueStubs }
  });
  (wrapper.vm as unknown as ConfigPanelVm).activeTab = 1;
  await flushPromises();
  return wrapper;
}

beforeEach(() => {
  setActivePinia(createTestingPinia());
});

describe('ConfigPanel env groups', () => {
  it('renders a plain group (the browser switches) as key/value rows', async () => {
    const wrapper = await mountEnvTab();

    const plain = wrapper.get('[data-test="env-group-SHERRY_BROWSER"]');
    expect(plain.text()).toContain('SHERRY_BROWSER_AGENT_ENABLED');
    expect(plain.text()).toContain('SHERRY_BROWSER_HEADLESS');
    // No model-profile manager inside a plain group.
    expect(wrapper.findAll('[data-test="model-profile-SHERRY_BROWSER"]')).toHaveLength(0);
  });

  it('still gives a model group its profile manager', async () => {
    const wrapper = await mountEnvTab();

    // A model group renders through the profile manager, never as a plain card.
    expect(wrapper.findAll('[data-test="model-profile-MAIN_LLM"]').length).toBeGreaterThan(0);
    expect(wrapper.findAll('[data-test="env-group-MAIN_LLM"]')).toHaveLength(0);
  });
});
