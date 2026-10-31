/**
 * Integration tests for the ConfigPanel MAX_TOKEN guard (backend mocked).
 *
 * Mounts the real ConfigPanel (the system-config right-sidebar tab) with the env
 * transport mocked: a sub-200K token
 * value must be rejected BEFORE any PUT (specific error shown, nothing written),
 * while a valid value is persisted and the model-config cache is invalidated.
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
      name: 'MAIN_LLM',
      entries: [
        { key: 'MAIN_LLM_MAX_TOKEN', value: '200000', value_edited: false },
        { key: 'MAIN_LLM_NAME', value: 'test-model', value_edited: false }
      ]
    }
  ];
  return { groups };
});
// The model families live in `.models` now (GET/PUT /models-config); the other
// two sources are stubbed empty so the panel's merge yields just this group.
const readModelsConfigMock = vi.hoisted(() =>
  vi.fn(async (): Promise<EnvConfigPayload> => ({ groups: envState.groups }))
);
const writeModelsConfigMock = vi.hoisted(() => vi.fn(async () => true));
const invalidateModelConfigCacheMock = vi.hoisted(() => vi.fn());

vi.mock('@/composables/env', () => ({
  readEnvConfig: vi.fn(async (): Promise<EnvConfigPayload> => ({ groups: [] })),
  readModelsConfig: readModelsConfigMock,
  readNetworkConfig: vi.fn(async (): Promise<EnvConfigPayload> => ({ groups: [] })),
  writeEnvConfig: vi.fn(async () => true),
  writeModelsConfig: writeModelsConfigMock,
  writeNetworkConfig: vi.fn(async () => true)
}));

vi.mock('@/composables/model-config', () => ({
  invalidateModelConfigCache: invalidateModelConfigCacheMock
}));

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
  ProgressSpinner: { name: 'ProgressSpinner', template: '<div class="ps"></div>' },
  TokenBudgetPanel: { name: 'TokenBudgetPanel', props: ['sessionId'], template: '<div class="tbp"></div>' },
  ShortcutSettingsSection: { name: 'ShortcutSettingsSection', template: '<div class="shortcut-section"></div>' }
};

interface ConfigPanelVm {
  activeTab: number;
  envGroups: EnvGroup[];
  envLoadError: string;
  handleSave: () => Promise<void>;
}

const MAIN_TOKEN_KEY = 'MAIN_LLM_MAX_TOKEN';

function tokenEntry(vm: ConfigPanelVm) {
  const entry = vm.envGroups.flatMap(group => group.entries).find(e => e.key === MAIN_TOKEN_KEY);
  if (!entry) throw new Error(`${MAIN_TOKEN_KEY} entry not loaded`);
  return entry;
}

/** Mount the dialog and load the env tab through the real watch + loader path. */
async function mountWithEnvTab() {
  const wrapper = mount(ConfigPanel, {
    props: { modelValue: true },
    global: { stubs: primevueStubs }
  });
  const vm = wrapper.vm as unknown as ConfigPanelVm;

  // Tabs are now [background, env, sherry]: the role config moved to the preset panel.
  vm.activeTab = 1;
  await flushPromises();

  expect(vm.envGroups).toHaveLength(1);
  return { wrapper, vm };
}

beforeEach(() => {
  envState.groups[0]!.entries[0]!.value = '200000';
  readModelsConfigMock.mockClear();
  writeModelsConfigMock.mockClear();
  invalidateModelConfigCacheMock.mockClear();
  // Real stores (chat background) are consumed by ConfigPanel; actions are
  // stubbed so saving never touches Dexie.
  setActivePinia(createTestingPinia());
});

describe('ConfigPanel per-area saving', () => {
  it('hides the footer on the env tab (each item saves on its own) but keeps it elsewhere', async () => {
    const { wrapper, vm } = await mountWithEnvTab();
    // Env tab: no Save/Cancel footer — each non-model group card and the
    // model panels' Save/Apply replace them.
    expect(wrapper.findAll('button').filter(b => b.text() === '取消')).toHaveLength(0);
    // handleSave is the env-area handler behind each non-model group button.
    expect(typeof (wrapper.vm as unknown as { handleSave?: unknown }).handleSave).toBe('function');

    // Background tab: the shared footer is back.
    vm.activeTab = 0;
    await flushPromises();
    expect(wrapper.findAll('button').filter(b => b.text() === '取消')).toHaveLength(1);
    expect(wrapper.findAll('button').filter(b => b.text() === '保存')).toHaveLength(1);
  });
});

describe('ConfigPanel MAX_TOKEN guard', () => {
  it('keeps the 200K rule off the tab header (it sits on the guarded inputs)', async () => {
    const { wrapper } = await mountWithEnvTab();

    // The tab no longer repeats the rule as a banner above every group; the
    // hint is rendered per guarded input by LlmModelManager.
    expect(wrapper.find('.border-amber-300').exists()).toBe(false);
  });

  it('rejects a sub-200K value with the key-specific error and writes nothing', async () => {
    const { vm } = await mountWithEnvTab();
    tokenEntry(vm).value = '65536';

    await vm.handleSave();

    expect(writeModelsConfigMock).not.toHaveBeenCalled();
    expect(invalidateModelConfigCacheMock).not.toHaveBeenCalled();
    expect(vm.envLoadError).toBe(`${MAIN_TOKEN_KEY} 必须 >= 200000 (200K)，当前值不满足要求，无法保存。`);
  });

  it('persists a valid value and invalidates the model-config cache', async () => {
    const { vm } = await mountWithEnvTab();
    tokenEntry(vm).value = '262144';

    await vm.handleSave();

    expect(writeModelsConfigMock).toHaveBeenCalledTimes(1);
    expect(writeModelsConfigMock).toHaveBeenCalledWith({ [MAIN_TOKEN_KEY]: '262144' });
    expect(invalidateModelConfigCacheMock).toHaveBeenCalledTimes(1);
    expect(vm.envLoadError).toBe('');
  });
});


describe('ConfigPanel shortcut-bindings tab', () => {
  it('renders the bindings section and hides the shared footer there', async () => {
    const wrapper = mount(ConfigPanel, {
      props: { modelValue: true },
      global: { stubs: primevueStubs }
    });
    const vm = wrapper.vm as unknown as ConfigPanelVm;

    // The tab body is the (stubbed) section; it saves on its own, so the
    // footer's Save/Cancel pair must be gone while it is active.
    expect(wrapper.find('.shortcut-section').exists()).toBe(true);
    vm.activeTab = 5;
    await flushPromises();
    expect(wrapper.findAll('button').filter(b => b.text() === '取消')).toHaveLength(0);
    expect(wrapper.findAll('button').filter(b => b.text() === '保存')).toHaveLength(0);
  });
});
