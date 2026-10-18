/**
 * The settings panel renders a group by its declared KIND (backend-mocked).
 *
 * The 模型配置 tab merges the model families from `.models` (GET
 * /models-config) and the remaining `.network` groups (GET /network-config).
 * LANGSMITH and SHERRY_BROWSER (from `.network`) plus the `.env` catch-all
 * `other` moved to the 应用配置 tab, still as key/value cards. Model groups
 * (MAIN_LLM, EMBEDDING, …) render the provider/model/api_key profile manager.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { setActivePinia } from 'pinia';
import { createTestingPinia } from '@pinia/testing';
import type { EnvConfigPayload, EnvGroup } from '@/composables/env';
import { emit } from '~/composables/mitt';
import ConfigPanel from '@/pages/home/components/ConfigPanel.vue';

const envState = vi.hoisted(() => {
  const networkGroups: EnvGroup[] = [
    {
      name: 'SHERRY_BROWSER',
      kind: 'plain',
      entries: [
        { key: 'SHERRY_BROWSER_AGENT_ENABLED', value: '1', value_edited: false },
        { key: 'SHERRY_BROWSER_HEADLESS', value: '1', value_edited: false }
      ]
    },
    {
      name: 'LANGSMITH',
      kind: 'plain',
      entries: [{ key: 'LANGSMITH_TRACING', value: 'false', value_edited: false }]
    }
  ];
  const envGroups: EnvGroup[] = [
    {
      name: 'other',
      kind: 'plain',
      entries: [{ key: 'SHERRY_AUTH_ENABLED', value: '0', value_edited: false }]
    }
  ];
  const modelGroups: EnvGroup[] = [
    {
      name: 'MAIN_LLM',
      kind: 'model',
      entries: [{ key: 'MAIN_LLM_NAME', value: 'deepseek-flash', value_edited: false }]
    }
  ];
  return { modelGroups, networkGroups, envGroups };
});

vi.mock('@/composables/env', () => ({
  API_BASE_URL: 'http://localhost:8080',
  WS_BASE_URL: 'ws://localhost:8080',
  readEnvConfig: vi.fn(async (): Promise<EnvConfigPayload> => ({ groups: envState.envGroups })),
  readModelsConfig: vi.fn(async (): Promise<EnvConfigPayload> => ({ groups: envState.modelGroups })),
  readNetworkConfig: vi.fn(async (): Promise<EnvConfigPayload> => ({ groups: envState.networkGroups })),
  writeEnvConfig: vi.fn(async () => true),
  writeModelsConfig: vi.fn(async () => true),
  writeNetworkConfig: vi.fn(async () => true)
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
  it('renders the moved browser switches as key/value rows in App Config', async () => {
    const wrapper = await mountEnvTab();

    const plain = wrapper.get('[data-test="app-config-group-SHERRY_BROWSER"]');
    expect(plain.text()).toContain('SHERRY_BROWSER_AGENT_ENABLED');
    expect(plain.text()).toContain('SHERRY_BROWSER_HEADLESS');
    // No model-profile manager inside a plain group.
    expect(wrapper.findAll('[data-test="model-profile-SHERRY_BROWSER"]')).toHaveLength(0);
    expect(wrapper.find('[data-test="env-group-SHERRY_BROWSER"]').exists()).toBe(false);
  });

  it('still gives a model group its profile manager', async () => {
    const wrapper = await mountEnvTab();

    // A model group renders through the profile manager, never as a plain card.
    expect(wrapper.findAll('[data-test="model-profile-MAIN_LLM"]').length).toBeGreaterThan(0);
    expect(wrapper.findAll('[data-test="env-group-MAIN_LLM"]')).toHaveLength(0);
  });

  it('moves LANGSMITH, SHERRY_BROWSER and the .env catch-all to App Config', async () => {
    const wrapper = await mountEnvTab();

    expect(wrapper.find('[data-test="env-group-LANGSMITH"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="env-group-SHERRY_BROWSER"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="env-group-other"]').exists()).toBe(false);
    expect(wrapper.get('[data-test="app-config-group-LANGSMITH"]').text()).toContain('LANGSMITH_TRACING');
    expect(wrapper.get('[data-test="app-config-group-other"]').text()).toContain('SHERRY_AUTH_ENABLED');
  });

  it('applies a pushed code-embedder download frame', async () => {
    const wrapper = await mountEnvTab();

    emit('ws:model_download_updated', {
      content: {
        kind: 'code_embedder',
        status: {
          profile: 'jina-embeddings-v2-base-code',
          license: 'Apache-2.0',
          natural_languages: ['en'],
          size_bytes: 641_517_466,
          downloaded_bytes: 269_000_000,
          total_bytes: 641_517_466,
          percent: 42,
          files_ready: false,
          enabled: false,
          state: 'downloading',
          error: null
        }
      }
    });
    await flushPromises();

    const card = wrapper.get('[data-test="code-embedder"]');
    expect(card.text()).toContain('42%');
    expect(card.find('.bg-sky-500').attributes('style')).toContain('width: 42%');
    wrapper.unmount();
  });
});
