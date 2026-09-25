import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount } from '@vue/test-utils';
import LlmModelManager from '@/pages/home/components/LlmModelManager.vue';
import type { LlmProfile } from '@/stores/llm-profiles';
import { MAX_PROFILES_PER_GROUP } from '@/stores/llm-profiles';

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

/** Fake (per-group) profiles store mirroring the real one's surface. */
let storeApi: {
  listFor: (group: string) => LlmProfile[];
  activeIdFor: (group: string) => string | null;
  migrateLegacyOnce: ReturnType<typeof vi.fn>;
  add: ReturnType<typeof vi.fn>;
  update: ReturnType<typeof vi.fn>;
  remove: ReturnType<typeof vi.fn>;
  setActive: ReturnType<typeof vi.fn>;
  byId: (group: string, id: string | null) => LlmProfile | undefined;
};

const makeStore = (profiles: LlmProfile[] = [], activeId: string | null = null, group = 'MAIN_LLM') => {
  const byGroup: Record<string, LlmProfile[]> = { [group]: profiles };
  const activeByGroup: Record<string, string | null> = { [group]: activeId };
  storeApi = {
    listFor: group => byGroup[group] ?? [],
    activeIdFor: group => activeByGroup[group] ?? null,
    trimGroup: vi.fn(),
    add: vi.fn((group: string, label: string, params: Record<string, string>) => {
      const id = `new-${(byGroup[group] ?? []).length + 1}`;
      byGroup[group] = [...(byGroup[group] ?? []), { id, label, params }];
      return id;
    }),
    update: vi.fn(),
    remove: vi.fn(),
    setActive: vi.fn(),
    byId: (group: string, id: string | null) =>
      id === null ? undefined : (byGroup[group] ?? []).find(p => p.id === id)
  };
  vi.stubGlobal(
    'useLlmProfilesStore',
    vi.fn(() => storeApi)
  );
};

const stubs = {
  Button: {
    props: ['label', 'disabled', 'title'],
    emits: ['click'],
    template: '<button class="btn" :disabled="disabled" :title="title" @click="$emit(\'click\')">{{ label }}</button>'
  },
  InputText: {
    props: ['modelValue', 'disabled'],
    emits: ['update:modelValue'],
    template:
      '<input class="inp" :value="modelValue" :disabled="disabled" @input="$emit(\'update:modelValue\', $event.target.value)" />'
  }
};

const mountPanel = (group = 'MAIN_LLM', keys: string[] = KEYS, values: Record<string, string> = ENV_VALUES) =>
  mount(LlmModelManager, {
    props: { group, keys, values, groupTitle: group },
    global: { stubs }
  });

describe('LlmModelManager.vue (integration, store stubbed)', () => {
  beforeEach(() => {
    makeStore();
  });

  it('works for other groups with their own key names (generic identity inference)', () => {
    // ITTT has the odd `ITTT_model_PROVIDER` casing and an `_API_NAME` name key.
    const ITTT_KEYS = ['ITTT_MODEL_LOCAL', 'ITTT_model_PROVIDER', 'ITTT_API_NAME', 'ITTT_API_BASE', 'ITTT_API_KEY'];
    const ITTT_VALUES: Record<string, string> = {
      ITTT_MODEL_LOCAL: 'false',
      ITTT_model_PROVIDER: 'openai',
      ITTT_API_NAME: 'gpt-4o-mini',
      ITTT_API_BASE: 'https://example.invalid/v1',
      ITTT_API_KEY: 'sk-ittt'
    };
    makeStore(
      [
        { id: 't1', label: 'other', params: { ...ITTT_VALUES, ITTT_API_NAME: 'other' } },
        { id: 't2', label: 'current', params: { ...ITTT_VALUES } }
      ],
      null,
      'ITTT'
    );
    const wrapper = mountPanel('ITTT', ITTT_KEYS, ITTT_VALUES);
    // ITTT has a local flag, so the list also carries the pinned built-in entry:
    // address the profile rows by label instead of by position.
    const rows = wrapper.findAll('[role="button"]');
    const rowFor = (label: string) => rows.find(r => r.text().includes(label))!;
    expect(rowFor('current').find('[data-active]').attributes('data-active')).toBe('true');
    expect(rowFor('other').find('[data-active]').attributes('data-active')).toBe('false');
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
    expect(storeApi.add).toHaveBeenCalledWith('MAIN_LLM', 'glm-5.3-flash', ENV_VALUES);
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
    const [group, id, patch] = storeApi.update.mock.calls[0]!;
    expect(group).toBe('MAIN_LLM');
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

  it('pins a built-in local-model entry above the saved profiles', () => {
    const LOCAL_KEYS = ['EMBEDDING_MODEL_LOCAL', 'EMBEDDING_MODEL_PROVIDER', 'EMBEDDING_API_NAME'];
    const LOCAL_VALUES: Record<string, string> = {
      EMBEDDING_MODEL_LOCAL: 'false',
      EMBEDDING_MODEL_PROVIDER: 'openai',
      EMBEDDING_API_NAME: 'bge-m3'
    };
    makeStore([{ id: 'e1', label: 'emb', params: { ...LOCAL_VALUES } }], 'e1', 'EMBEDDING');
    const wrapper = mountPanel('EMBEDDING', LOCAL_KEYS, LOCAL_VALUES);
    const rows = wrapper.findAll('[role="button"]');
    expect(rows).toHaveLength(2);
    expect(rows[0]!.text()).toContain('本地模型');
    expect(rows[1]!.text()).toContain('emb');
  });

  it('the local entry shows the group parameters read-only, with 应用 but no 保存', async () => {
    const LOCAL_KEYS = ['EMBEDDING_MODEL_LOCAL', 'EMBEDDING_API_NAME'];
    const LOCAL_VALUES: Record<string, string> = {
      EMBEDDING_MODEL_LOCAL: 'false',
      EMBEDDING_API_NAME: 'bge-m3'
    };
    makeStore([{ id: 'e1', label: 'emb', params: { ...LOCAL_VALUES } }], 'e1', 'EMBEDDING');
    const wrapper = mountPanel('EMBEDDING', LOCAL_KEYS, LOCAL_VALUES);
    await wrapper.findAll('[role="button"]')[0]!.trigger('click'); // select the local entry

    // parameters are rendered but disabled (read-only) — and never include the
    // local flag input (it is derived from which entry is applied)
    const inputs = wrapper.findAll('input.inp');
    expect(inputs).toHaveLength(LOCAL_KEYS.length - 1);
    expect(inputs.every(i => i.attributes('disabled') !== undefined)).toBe(true);
    // option 2: local mode ignores the API parameters, so they display EMPTY
    expect(inputs.every(i => (i.attributes('value') ?? '') === '')).toBe(true);
    // 应用 present, 保存 absent
    const labels = wrapper.findAll('button.btn').map(b => b.text());
    expect(labels).toContain('应用');
    expect(labels).not.toContain('保存');
  });

  it('applying the local entry emits the group params with the flag forced on', async () => {
    const LOCAL_KEYS = ['EMBEDDING_MODEL_LOCAL', 'EMBEDDING_API_NAME'];
    const LOCAL_VALUES: Record<string, string> = {
      EMBEDDING_MODEL_LOCAL: 'false',
      EMBEDDING_API_NAME: 'bge-m3'
    };
    makeStore([{ id: 'e1', label: 'emb', params: { ...LOCAL_VALUES } }], 'e1', 'EMBEDDING');
    const wrapper = mountPanel('EMBEDDING', LOCAL_KEYS, LOCAL_VALUES);
    await wrapper.findAll('[role="button"]')[0]!.trigger('click');
    const applyButton = wrapper.findAll('button.btn').find(b => b.text() === '应用');
    await applyButton!.trigger('click');
    const payload = wrapper.emitted('apply')![0]![0] as { id: string; params: Record<string, string> };
    expect(payload.id).toBe('builtin:local');
    // ONLY the flag: the API keys stay untouched in .env (local mode ignores
    // them, and switching back to a cloud model must keep the old values).
    expect(payload.params).toEqual({ EMBEDDING_MODEL_LOCAL: 'true' });
  });

  it('applying a saved profile forces the local flag to false', async () => {
    const LOCAL_KEYS = ['EMBEDDING_MODEL_LOCAL', 'EMBEDDING_API_NAME'];
    const LOCAL_VALUES: Record<string, string> = {
      EMBEDDING_MODEL_LOCAL: 'true',
      EMBEDDING_API_NAME: 'bge-m3'
    };
    // the stored profile even CLAIMS local=true; the applied entry decides otherwise
    makeStore([{ id: 'e1', label: 'remote', params: { ...LOCAL_VALUES } }], 'e1', 'EMBEDDING');
    const wrapper = mountPanel('EMBEDDING', LOCAL_KEYS, LOCAL_VALUES);
    // no flag input anywhere
    expect(wrapper.findAll('input.inp')).toHaveLength(1);
    const applyButton = wrapper.findAll('button.btn').find(b => b.text() === '应用');
    await applyButton!.trigger('click');
    const payload = wrapper.emitted('apply')![0]![0] as { id: string; params: Record<string, string> };
    expect(payload.id).toBe('e1');
    expect(payload.params.EMBEDDING_MODEL_LOCAL).toBe('false');
    expect(payload.params.EMBEDDING_API_NAME).toBe('bge-m3');
  });

  it('disables 添加模型 at the cap and trims oversized storage on setup', () => {
    const many: LlmProfile[] = Array.from({ length: MAX_PROFILES_PER_GROUP }, (_, i) => ({
      id: `p${i}`,
      label: `m${i}`,
      params: { TTI_API_NAME: `m${i}` }
    }));
    makeStore(many, null, 'TTI');
    const wrapper = mountPanel('TTI', ['TTI_API_NAME'], { TTI_API_NAME: 'x' });
    // storage cap enforced through the store, not just the button
    expect(storeApi.trimGroup).toHaveBeenCalledWith('TTI');
    const addButton = wrapper.findAll('button.btn').find(b => b.text() === '添加模型');
    expect(addButton!.attributes('disabled')).toBeDefined();
    expect(addButton!.attributes('title')).toContain(String(MAX_PROFILES_PER_GROUP));
  });

  it('keeps 添加模型 enabled below the cap', () => {
    makeStore([{ id: 'p1', label: 'a', params: {} }], null, 'TTI');
    const wrapper = mountPanel('TTI', ['TTI_API_NAME'], { TTI_API_NAME: 'x' });
    const addButton = wrapper.findAll('button.btn').find(b => b.text() === '添加模型');
    expect(addButton!.attributes('disabled')).toBeUndefined();
  });

  it('delete removes the profile and auto-applies the PREVIOUS entry', async () => {
    const KEYS = ['TTI_MODEL_PROVIDER', 'TTI_API_NAME'];
    const VALUES: Record<string, string> = { TTI_MODEL_PROVIDER: 'openai', TTI_API_NAME: 'tti' };
    makeStore(
      [
        { id: 'p1', label: 'first', params: { ...VALUES, TTI_API_NAME: 'first' } },
        { id: 'p2', label: 'second', params: { ...VALUES, TTI_API_NAME: 'second' } }
      ],
      'p2',
      'TTI'
    );
    const wrapper = mountPanel('TTI', KEYS, VALUES);
    // select the second row, delete it
    await wrapper.findAll('[role="button"]')[1]!.trigger('click');
    const del = wrapper.findAll('button.btn').find(b => b.text() === '删除');
    expect(del).toBeTruthy();
    await del!.trigger('click');
    expect(storeApi.remove).toHaveBeenCalledWith('TTI', 'p2');
    // the previous entry (p1) is applied automatically
    const emitted = wrapper.emitted('apply');
    expect(emitted).toHaveLength(1);
    const payload = emitted![0]![0] as { id: string; params: Record<string, string> };
    expect(payload.id).toBe('p1');
    expect(payload.params.TTI_API_NAME).toBe('first');
    // and it becomes the viewed entry
    expect((wrapper.findAll('input.inp')[1]!.element as HTMLInputElement).value).toBe('first');
  });

  it('offers no delete button for the built-in local entry', async () => {
    const KEYS = ['EMBEDDING_MODEL_LOCAL', 'EMBEDDING_API_NAME'];
    const VALUES: Record<string, string> = { EMBEDDING_MODEL_LOCAL: 'true', EMBEDDING_API_NAME: 'bge-m3' };
    makeStore([{ id: 'e1', label: 'emb', params: { ...VALUES } }], 'e1', 'EMBEDDING');
    const wrapper = mountPanel('EMBEDDING', KEYS, VALUES);
    await wrapper.findAll('[role="button"]')[0]!.trigger('click'); // the local entry
    expect(wrapper.findAll('button.btn').some(b => b.text() === '删除')).toBe(false);
  });

  it('scrolls the saved-profile list when it overflows', () => {
    makeStore([{ id: 'p1', label: 'a', params: { TTI_API_NAME: 'a' } }], 'p1', 'TTI');
    const wrapper = mountPanel('TTI', ['TTI_API_NAME'], { TTI_API_NAME: 'a' });
    const scroller = wrapper.find('.overflow-y-auto');
    expect(scroller.exists()).toBe(true);
    expect(scroller.classes()).toContain('max-h-56');
  });

  it('renders no local entry for a group without a local flag', () => {
    makeStore([{ id: 't1', label: 'tti', params: { TTI_API_NAME: 'tti' } }], 't1', 'TTI');
    const wrapper = mountPanel('TTI', ['TTI_MODEL_PROVIDER', 'TTI_API_NAME'], {
      TTI_MODEL_PROVIDER: 'openai',
      TTI_API_NAME: 'tti'
    });
    const rows = wrapper.findAll('[role="button"]');
    expect(rows).toHaveLength(1);
    expect(rows[0]!.text()).toContain('tti');
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
