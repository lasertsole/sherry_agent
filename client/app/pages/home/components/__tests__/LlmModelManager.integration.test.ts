import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount } from '@vue/test-utils';
import LlmModelManager from '@/pages/home/components/LlmModelManager.vue';
import type { LlmProfile } from '@/stores/llm-profiles';

const KEYS = [
  'MAIN_LLM_PROVIDER',
  'MAIN_LLM_NAME',
  'MAIN_LLM_API_BASE',
  'MAIN_LLM_API_KEY',
  'MAIN_LLM_MAX_TOKEN',
  'MAIN_LLM_ENABLE_THINKING'
];

const ENV_VALUES: Record<string, string> = {
  MAIN_LLM_PROVIDER: 'openai',
  MAIN_LLM_NAME: 'glm-5.3-flash',
  MAIN_LLM_API_BASE: 'https://example.invalid/v4',
  MAIN_LLM_API_KEY: 'sk-env',
  MAIN_LLM_MAX_TOKEN: '131072',
  MAIN_LLM_ENABLE_THINKING: 'true'
};

/** Fake profiles store mirroring the real one's surface. */
let storeApi: {
  profiles: LlmProfile[];
  activeId: string | null;
  add: ReturnType<typeof vi.fn>;
  update: ReturnType<typeof vi.fn>;
  remove: ReturnType<typeof vi.fn>;
  setActive: ReturnType<typeof vi.fn>;
  byId: (id: string | null) => LlmProfile | undefined;
};

const makeStore = (profiles: LlmProfile[] = [], activeId: string | null = null) => {
  storeApi = {
    profiles,
    activeId,
    add: vi.fn((label: string, params: Record<string, string>) => {
      const id = `new-${profiles.length + 1}`;
      profiles = [...profiles, { id, label, params }];
      storeApi.profiles = profiles;
      return id;
    }),
    update: vi.fn(),
    remove: vi.fn(),
    setActive: vi.fn(),
    byId: (id: string | null) => (id === null ? undefined : profiles.find(p => p.id === id))
  };
  vi.stubGlobal(
    'useLlmProfilesStore',
    vi.fn(() => storeApi)
  );
};

const stubs = {
  Button: {
    props: ['label'],
    emits: ['click'],
    template: '<button class="btn" @click="$emit(\'click\')">{{ label }}</button>'
  },
  InputText: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<input class="inp" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />'
  }
};

const mountPanel = () =>
  mount(LlmModelManager, {
    props: { keys: KEYS, values: ENV_VALUES, groupTitle: '模型列表 · MAIN_LLM' },
    global: { stubs }
  });

describe('LlmModelManager.vue (integration, store stubbed)', () => {
  beforeEach(() => {
    makeStore();
  });

  it('shows the add button and an empty hint without profiles', () => {
    const wrapper = mountPanel();
    expect(wrapper.text()).toContain('添加模型');
    expect(wrapper.text()).toContain('暂无模型');
    // no parameter inputs until a model exists
    expect(wrapper.findAll('input.inp')).toHaveLength(0);
  });

  it('add seeds a profile from the live .env values and selects it', async () => {
    const wrapper = mountPanel();
    const addButton = wrapper.findAll('button.btn').find(b => b.text() === '添加模型');
    await addButton!.trigger('click');
    expect(storeApi.add).toHaveBeenCalledWith('glm-5.3-flash', ENV_VALUES);
    // the new profile is selected: its parameter inputs render
    expect(wrapper.findAll('input.inp')).toHaveLength(KEYS.length);
  });

  it('renders one row per profile and marks only the active one with a dot', () => {
    makeStore(
      [
        { id: 'p1', label: 'glm-4.6', params: { ...ENV_VALUES, MAIN_LLM_NAME: 'glm-4.6' } },
        { id: 'p2', label: 'glm-5.3', params: { ...ENV_VALUES } }
      ],
      'p1'
    );
    const wrapper = mountPanel();
    const dots = wrapper.findAll('[data-active]');
    expect(dots).toHaveLength(2);
    expect(dots[0]!.attributes('data-active')).toBe('true');
    expect(dots[1]!.attributes('data-active')).toBe('false');
  });

  it('infers the dot from the live .env when no marker exists yet', () => {
    makeStore([
      { id: 'p1', label: 'other', params: { ...ENV_VALUES, MAIN_LLM_NAME: 'other' } },
      { id: 'p2', label: 'current', params: { ...ENV_VALUES } }
    ]);
    const wrapper = mountPanel();
    const dots = wrapper.findAll('[data-active]');
    expect(dots[0]!.attributes('data-active')).toBe('false');
    expect(dots[1]!.attributes('data-active')).toBe('true');
  });

  it('save writes the edited draft into the store (no apply)', async () => {
    makeStore([{ id: 'p1', label: 'glm-4.6', params: { ...ENV_VALUES, MAIN_LLM_NAME: 'glm-4.6' } }], 'p1');
    const wrapper = mountPanel();
    const inputs = wrapper.findAll('input.inp');
    await inputs[1]!.setValue('glm-4.7');
    const saveButton = wrapper.findAll('button.btn').find(b => b.text() === '保存');
    await saveButton!.trigger('click');
    expect(storeApi.update).toHaveBeenCalledTimes(1);
    const [id, patch] = storeApi.update.mock.calls[0]!;
    expect(id).toBe('p1');
    expect(patch.params.MAIN_LLM_NAME).toBe('glm-4.7');
    expect(patch.label).toBe('glm-4.7');
    expect(wrapper.emitted('apply')).toBeUndefined();
  });

  it('apply emits the draft parameters for the parent to write into .env', async () => {
    makeStore([{ id: 'p1', label: 'glm-4.6', params: { ...ENV_VALUES, MAIN_LLM_NAME: 'glm-4.6' } }], 'p1');
    const wrapper = mountPanel();
    const inputs = wrapper.findAll('input.inp');
    await inputs[1]!.setValue('glm-4.7');
    const applyButton = wrapper.findAll('button.btn').find(b => b.text() === '应用');
    await applyButton!.trigger('click');
    const emitted = wrapper.emitted('apply');
    expect(emitted).toHaveLength(1);
    expect(emitted![0]![0]).toMatchObject({ id: 'p1' });
    expect((emitted![0]![0] as { params: Record<string, string> }).params.MAIN_LLM_NAME).toBe('glm-4.7');
  });

  it('switching the selected model reloads the parameter draft', async () => {
    makeStore(
      [
        { id: 'p1', label: 'a', params: { ...ENV_VALUES, MAIN_LLM_NAME: 'a' } },
        { id: 'p2', label: 'b', params: { ...ENV_VALUES, MAIN_LLM_NAME: 'b' } }
      ],
      'p1'
    );
    const wrapper = mountPanel();
    const rows = wrapper.findAll('[role="button"]');
    await rows[1]!.trigger('click');
    const inputs = wrapper.findAll('input.inp');
    expect((inputs[1]!.element as HTMLInputElement).value).toBe('b');
  });
});
