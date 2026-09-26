/**
 * Integration tests for the ConfigDialog MAX_TOKEN guard (backend mocked).
 *
 * Mounts the real ConfigDialog with the env transport mocked: a sub-128K token
 * value must be rejected BEFORE any PUT (specific error shown, nothing written),
 * while a valid value is persisted and the model-config cache is invalidated.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { setActivePinia } from 'pinia';
import { createTestingPinia } from '@pinia/testing';
import type { EnvConfigPayload, EnvGroup } from '@/composables/env';
import ConfigDialog from '@/pages/home/components/ConfigDialog.vue';

const envState = vi.hoisted(() => {
  const groups: EnvGroup[] = [
    {
      name: 'MAIN_LLM',
      entries: [
        { key: 'MAIN_LLM_MAX_TOKEN', value: '131072', value_edited: false },
        { key: 'MAIN_LLM_NAME', value: 'test-model', value_edited: false }
      ]
    }
  ];
  return { groups };
});
const readEnvConfigMock = vi.hoisted(() => vi.fn(async (): Promise<EnvConfigPayload> => ({ groups: envState.groups })));
const writeEnvConfigMock = vi.hoisted(() => vi.fn(async () => true));
const invalidateModelConfigCacheMock = vi.hoisted(() => vi.fn());

vi.mock('@/composables/env', () => ({
  readEnvConfig: readEnvConfigMock,
  writeEnvConfig: writeEnvConfigMock
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
  ProgressSpinner: { name: 'ProgressSpinner', template: '<div class="ps"></div>' }
};

interface ConfigDialogVm {
  activeTab: number;
  envGroups: EnvGroup[];
  envLoadError: string;
  handleSave: () => Promise<void>;
}

const MAIN_TOKEN_KEY = 'MAIN_LLM_MAX_TOKEN';

function tokenEntry(vm: ConfigDialogVm) {
  const entry = vm.envGroups.flatMap(group => group.entries).find(e => e.key === MAIN_TOKEN_KEY);
  if (!entry) throw new Error(`${MAIN_TOKEN_KEY} entry not loaded`);
  return entry;
}

/** Mount the dialog and load the env tab through the real watch + loader path. */
async function mountWithEnvTab() {
  const wrapper = mount(ConfigDialog, {
    props: { modelValue: true },
    global: { stubs: primevueStubs }
  });
  const vm = wrapper.vm as unknown as ConfigDialogVm;

  vm.activeTab = 2;
  await flushPromises();

  expect(vm.envGroups).toHaveLength(1);
  return { wrapper, vm };
}

beforeEach(() => {
  envState.groups[0]!.entries[0]!.value = '131072';
  readEnvConfigMock.mockClear();
  writeEnvConfigMock.mockClear();
  invalidateModelConfigCacheMock.mockClear();
  // Real stores (chat background) are consumed by ConfigDialog; actions are
  // stubbed so saving never touches Dexie.
  setActivePinia(createTestingPinia());
});

describe('ConfigDialog per-area saving', () => {
  it('hides the footer on the env tab (each item saves on its own) but keeps it elsewhere', async () => {
    const { wrapper, vm } = await mountWithEnvTab();
    // Env tab: no footer 保存/取消 — the other group's own button and the
    // model panels' 保存/应用 replace them.
    expect(wrapper.findAll('button').filter(b => b.text() === '取消')).toHaveLength(0);
    // handleSave is the env-area handler behind that other-group button.
    expect(typeof (wrapper.vm as unknown as { handleSave?: unknown }).handleSave).toBe('function');

    // Character tab: the shared footer is back.
    vm.activeTab = 0;
    await flushPromises();
    expect(wrapper.findAll('button').filter(b => b.text() === '取消')).toHaveLength(1);
    expect(wrapper.findAll('button').filter(b => b.text() === '保存')).toHaveLength(1);
  });
});

describe('ConfigDialog MAX_TOKEN guard', () => {
  it('keeps the 128K rule off the tab header (it sits on the guarded inputs)', async () => {
    const { wrapper } = await mountWithEnvTab();

    // The tab no longer repeats the rule as a banner above every group; the
    // hint is rendered per guarded input by LlmModelManager.
    expect(wrapper.find('.border-amber-300').exists()).toBe(false);
  });

  it('rejects a sub-128K value with the key-specific error and writes nothing', async () => {
    const { vm } = await mountWithEnvTab();
    tokenEntry(vm).value = '65536';

    await vm.handleSave();

    expect(writeEnvConfigMock).not.toHaveBeenCalled();
    expect(invalidateModelConfigCacheMock).not.toHaveBeenCalled();
    expect(vm.envLoadError).toBe(`${MAIN_TOKEN_KEY} 必须 >= 131072 (128K)，当前值不满足要求，无法保存。`);
  });

  it('persists a valid value and invalidates the model-config cache', async () => {
    const { vm } = await mountWithEnvTab();
    tokenEntry(vm).value = '262144';

    await vm.handleSave();

    expect(writeEnvConfigMock).toHaveBeenCalledTimes(1);
    expect(writeEnvConfigMock).toHaveBeenCalledWith({ [MAIN_TOKEN_KEY]: '262144' });
    expect(invalidateModelConfigCacheMock).toHaveBeenCalledTimes(1);
    expect(vm.envLoadError).toBe('');
  });
});
