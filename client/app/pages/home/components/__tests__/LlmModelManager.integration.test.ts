import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount, type VueWrapper } from '@vue/test-utils';
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
  trimGroup: ReturnType<typeof vi.fn>;
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
    listFor: g => byGroup[g] ?? [],
    activeIdFor: g => activeByGroup[g] ?? null,
    trimGroup: vi.fn(),
    add: vi.fn((g: string, label: string, params: Record<string, string>) => {
      const id = `new-${(byGroup[g] ?? []).length + 1}`;
      byGroup[g] = [...(byGroup[g] ?? []), { id, label, params }];
      return id;
    }),
    update: vi.fn(),
    remove: vi.fn(),
    setActive: vi.fn(),
    byId: (g: string, id: string | null) => (id === null ? undefined : (byGroup[g] ?? []).find(p => p.id === id))
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

/**
 * Panels start collapsed: open the one under test through its toggle.
 * @param wrapper
 */
const expand = async (wrapper: VueWrapper) => {
  const toggle = wrapper.findAll('button').find(b => b.attributes('aria-label') === '展开');
  if (!toggle) throw new Error('collapse toggle not found');
  await toggle.trigger('click');
};

/**
 * The row button whose visible label matches (rows are labelled by API name).
 * @param wrapper
 * @param label
 */
const rowFor = (wrapper: VueWrapper, label: string) =>
  wrapper.findAll('[role="button"]').find(r => r.text().includes(label));

/**
 * The action button by label.
 * @param wrapper
 * @param label
 */
const buttonFor = (wrapper: VueWrapper, label: string) => wrapper.findAll('button.btn').find(b => b.text() === label);

/**
 * Number of 128K-floor hints rendered in the panel (one per guarded input).
 * @param wrapper
 */
const countHints = (wrapper: VueWrapper) => (wrapper.text().match(/必须 >= 131072/g) ?? []).length;

describe('LlmModelManager.vue (integration, store stubbed)', () => {
  beforeEach(() => {
    makeStore();
  });

  it('starts collapsed and reveals the manager on expand', async () => {
    makeStore([{ id: 'p1', label: 'a', params: { MAIN_LLM_NAME: 'a' } }]);
    const wrapper = mountPanel();
    // collapsed: neither the list nor the parameters are rendered
    expect(wrapper.findAll('[role="button"]')).toHaveLength(0);
    expect(wrapper.findAll('input.inp')).toHaveLength(0);

    await expand(wrapper);
    expect(rowFor(wrapper, 'a')).toBeTruthy();
    expect(wrapper.findAll('input.inp').length).toBeGreaterThan(0);
  });

  it('works for other groups with their own key names (generic identity inference)', async () => {
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
    await expand(wrapper);

    expect(rowFor(wrapper, 'gpt-4o-mini')!.find('[data-active]').attributes('data-active')).toBe('true');
    expect(rowFor(wrapper, 'other')!.find('[data-active]').attributes('data-active')).toBe('false');
  });

  it('shows the add button and an empty hint without profiles', async () => {
    const wrapper = mountPanel();
    await expand(wrapper);
    expect(wrapper.text()).toContain('添加模型');
    expect(wrapper.text()).toContain('暂无模型');
    expect(wrapper.findAll('input.inp')).toHaveLength(0);
  });

  it('add seeds a profile from the live .env values and selects it', async () => {
    const wrapper = mountPanel();
    await expand(wrapper);
    await buttonFor(wrapper, '添加模型')!.trigger('click');
    expect(storeApi.add).toHaveBeenCalledWith('MAIN_LLM', 'glm-5.3-flash', ENV_VALUES);
    expect(wrapper.findAll('input.inp')).toHaveLength(KEYS.length);
  });

  it('marks only the applied profile with the active marker', async () => {
    makeStore(
      [
        { id: 'p1', label: 'glm-4.6', params: { ...ENV_VALUES, MAIN_LLM_NAME: 'glm-4.6' } },
        { id: 'p2', label: 'glm-5.3', params: { ...ENV_VALUES, MAIN_LLM_NAME: 'glm-5.3' } }
      ],
      'p1'
    );
    const wrapper = mountPanel();
    await expand(wrapper);

    expect(rowFor(wrapper, 'glm-4.6')!.find('[data-active]').attributes('data-active')).toBe('true');
    expect(rowFor(wrapper, 'glm-5.3')!.find('[data-active]').attributes('data-active')).toBe('false');
  });

  it('infers the marker from the live .env when no marker exists yet', async () => {
    makeStore([
      { id: 'p1', label: 'other', params: { ...ENV_VALUES, MAIN_LLM_NAME: 'other' } },
      { id: 'p2', label: 'current', params: { ...ENV_VALUES } }
    ]);
    const wrapper = mountPanel();
    await expand(wrapper);

    expect(rowFor(wrapper, 'other')!.find('[data-active]').attributes('data-active')).toBe('false');
    expect(rowFor(wrapper, 'glm-5.3-flash')!.find('[data-active]').attributes('data-active')).toBe('true');
  });

  it('save writes the edited draft into the store (no apply)', async () => {
    makeStore([{ id: 'p1', label: 'glm-4.6', params: { ...ENV_VALUES, MAIN_LLM_NAME: 'glm-4.6' } }], 'p1');
    const wrapper = mountPanel();
    await expand(wrapper);
    await wrapper.findAll('input.inp')[1]!.setValue('glm-4.7');
    await buttonFor(wrapper, '保存')!.trigger('click');

    expect(storeApi.update).toHaveBeenCalledTimes(1);
    const [group, id, patch] = storeApi.update.mock.calls[0]!;
    expect(group).toBe('MAIN_LLM');
    expect(id).toBe('p1');
    expect(patch.params.MAIN_LLM_NAME).toBe('glm-4.7');
    expect(wrapper.emitted('apply')).toBeUndefined();
  });

  it('apply emits the draft parameters for the parent to write into .env', async () => {
    makeStore([{ id: 'p1', label: 'glm-4.6', params: { ...ENV_VALUES, MAIN_LLM_NAME: 'glm-4.6' } }], 'p1');
    const wrapper = mountPanel();
    await expand(wrapper);
    await wrapper.findAll('input.inp')[1]!.setValue('glm-4.7');
    await buttonFor(wrapper, '应用')!.trigger('click');

    const emitted = wrapper.emitted('apply');
    expect(emitted).toHaveLength(1);
    const payload = emitted![0]![0] as { id: string; params: Record<string, string> };
    expect(payload.id).toBe('p1');
    expect(payload.params.MAIN_LLM_NAME).toBe('glm-4.7');
  });

  it('the local entry shows the group parameters read-only, with 应用 but no 保存', async () => {
    const LOCAL_KEYS = ['EMBEDDING_MODEL_LOCAL', 'EMBEDDING_API_NAME'];
    const LOCAL_VALUES: Record<string, string> = {
      EMBEDDING_MODEL_LOCAL: 'false',
      EMBEDDING_API_NAME: 'bge-m3'
    };
    makeStore([{ id: 'e1', label: 'emb', params: { ...LOCAL_VALUES } }], 'e1', 'EMBEDDING');
    const wrapper = mountPanel('EMBEDDING', LOCAL_KEYS, LOCAL_VALUES);
    await expand(wrapper);
    await rowFor(wrapper, '本地模型')!.trigger('click');

    const inputs = wrapper.findAll('input.inp');
    expect(inputs).toHaveLength(LOCAL_KEYS.length - 1);
    expect(inputs.every(i => i.attributes('disabled') !== undefined)).toBe(true);
    expect(inputs.every(i => (i.attributes('value') ?? '') === '')).toBe(true);
    expect(buttonFor(wrapper, '应用')).toBeTruthy();
    expect(buttonFor(wrapper, '保存')).toBeUndefined();
  });

  it('applying the local entry emits only the flag', async () => {
    const LOCAL_KEYS = ['EMBEDDING_MODEL_LOCAL', 'EMBEDDING_API_NAME'];
    const LOCAL_VALUES: Record<string, string> = {
      EMBEDDING_MODEL_LOCAL: 'false',
      EMBEDDING_API_NAME: 'bge-m3'
    };
    makeStore([{ id: 'e1', label: 'emb', params: { ...LOCAL_VALUES } }], 'e1', 'EMBEDDING');
    const wrapper = mountPanel('EMBEDDING', LOCAL_KEYS, LOCAL_VALUES);
    await expand(wrapper);
    await rowFor(wrapper, '本地模型')!.trigger('click');
    await buttonFor(wrapper, '应用')!.trigger('click');

    const payload = wrapper.emitted('apply')![0]![0] as { id: string; params: Record<string, string> };
    expect(payload.id).toBe('builtin:local');
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
    await expand(wrapper);
    await buttonFor(wrapper, '应用')!.trigger('click');

    const payload = wrapper.emitted('apply')![0]![0] as { id: string; params: Record<string, string> };
    expect(payload.id).toBe('e1');
    expect(payload.params.EMBEDDING_MODEL_LOCAL).toBe('false');
    expect(payload.params.EMBEDDING_API_NAME).toBe('bge-m3');
  });

  it('trims oversized storage on setup and disables 添加模型 at the cap', async () => {
    const many: LlmProfile[] = Array.from({ length: MAX_PROFILES_PER_GROUP }, (_, i) => ({
      id: `p${i}`,
      label: `m${i}`,
      params: { TTI_API_NAME: `m${i}` }
    }));
    makeStore(many, null, 'TTI');
    const wrapper = mountPanel('TTI', ['TTI_API_NAME'], { TTI_API_NAME: 'x' });
    await expand(wrapper);

    // storage cap enforced through the store, not just the button
    expect(storeApi.trimGroup).toHaveBeenCalledWith('TTI');
    expect(buttonFor(wrapper, '添加模型')!.attributes('disabled')).toBeDefined();
    // the budget readout counts the saved entries against the cap
    expect(wrapper.text()).toContain(`${MAX_PROFILES_PER_GROUP} / ${MAX_PROFILES_PER_GROUP}`);
  });

  it('keeps 添加模型 enabled below the cap', async () => {
    makeStore([{ id: 'p1', label: 'a', params: {} }], null, 'TTI');
    const wrapper = mountPanel('TTI', ['TTI_API_NAME'], { TTI_API_NAME: 'x' });
    await expand(wrapper);
    expect(buttonFor(wrapper, '添加模型')!.attributes('disabled')).toBeUndefined();
    expect(wrapper.text()).toContain(`1 / ${MAX_PROFILES_PER_GROUP}`);
  });

  it('counts only the saved entries, never the built-in local one', async () => {
    const LOCAL_KEYS = ['EMBEDDING_MODEL_LOCAL', 'EMBEDDING_API_NAME'];
    const LOCAL_VALUES: Record<string, string> = {
      EMBEDDING_MODEL_LOCAL: 'false',
      EMBEDDING_API_NAME: 'bge-m3'
    };
    makeStore([{ id: 'e1', label: 'emb', params: { ...LOCAL_VALUES } }], 'e1', 'EMBEDDING');
    const wrapper = mountPanel('EMBEDDING', LOCAL_KEYS, LOCAL_VALUES);
    await expand(wrapper);
    // 1 saved profile + the pinned local entry rendered, counter still 1
    expect(rowFor(wrapper, '本地模型')).toBeTruthy();
    expect(rowFor(wrapper, 'bge-m3')).toBeTruthy();
    expect(wrapper.text()).toContain(`1 / ${MAX_PROFILES_PER_GROUP}`);
  });

  it('delete removes the profile and auto-applies the PREVIOUS entry', async () => {
    vi.stubGlobal('useConfirm', () => ({ require: (o?: { accept?: () => void }) => o?.accept?.() }));
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
    await expand(wrapper);
    await rowFor(wrapper, 'second')!.trigger('click');
    await buttonFor(wrapper, '删除')!.trigger('click');

    expect(storeApi.remove).toHaveBeenCalledWith('TTI', 'p2');
    const emitted = wrapper.emitted('apply');
    expect(emitted).toHaveLength(1);
    const payload = emitted![0]![0] as { id: string; params: Record<string, string> };
    expect(payload.id).toBe('p1');
    expect(payload.params.TTI_API_NAME).toBe('first');
  });

  it('blocks 保存/应用 while the provider or API name is empty', async () => {
    const KEYS = ['TTI_MODEL_PROVIDER', 'TTI_API_NAME'];
    const VALUES: Record<string, string> = { TTI_MODEL_PROVIDER: '', TTI_API_NAME: '' };
    makeStore([{ id: 'p1', label: 'x', params: { ...VALUES } }], 'p1', 'TTI');
    const wrapper = mountPanel('TTI', KEYS, VALUES);
    await expand(wrapper);

    await buttonFor(wrapper, '应用')!.trigger('click');
    await buttonFor(wrapper, '保存')!.trigger('click');
    expect(wrapper.emitted('apply')).toBeUndefined();
    expect(storeApi.update).not.toHaveBeenCalled();
    expect(wrapper.text()).toContain('提供商与模型 API 名为必填项');

    const inputs = wrapper.findAll('input.inp');
    await inputs[0]!.setValue('openai');
    await inputs[1]!.setValue('gpt-4o-mini');
    await buttonFor(wrapper, '应用')!.trigger('click');
    const payload = wrapper.emitted('apply')![0]![0] as { params: Record<string, string> };
    expect(payload.params.TTI_API_NAME).toBe('gpt-4o-mini');
  });

  it('labels rows with the model API name, falling back to the stored label', async () => {
    makeStore(
      [
        { id: 'p1', label: 'stale label', params: { TTI_API_NAME: 'real-model-name' } },
        { id: 'p2', label: 'fallback', params: { TTI_API_NAME: '' } }
      ],
      'p1',
      'TTI'
    );
    const wrapper = mountPanel('TTI', ['TTI_API_NAME'], { TTI_API_NAME: 'x' });
    await expand(wrapper);

    expect(rowFor(wrapper, 'real-model-name')).toBeTruthy();
    expect(rowFor(wrapper, 'stale label')).toBeUndefined();
    expect(rowFor(wrapper, 'fallback')).toBeTruthy();
  });

  it('asks for confirmation before deleting, and a rejection keeps the profile', async () => {
    const requireSpy = vi.fn();
    vi.stubGlobal('useConfirm', () => ({ require: requireSpy }));
    makeStore(
      [
        { id: 'p1', label: 'a', params: { TTI_API_NAME: 'a' } },
        { id: 'p2', label: 'b', params: { TTI_API_NAME: 'b' } }
      ],
      'p2',
      'TTI'
    );
    const wrapper = mountPanel('TTI', ['TTI_API_NAME'], { TTI_API_NAME: 'x' });
    await expand(wrapper);
    await rowFor(wrapper, 'b')!.trigger('click');
    await buttonFor(wrapper, '删除')!.trigger('click');

    expect(requireSpy).toHaveBeenCalledTimes(1);
    const options = requireSpy.mock.calls[0]![0] as { message: string; accept: () => void };
    expect(options.message).toContain('确定删除模型');
    expect(storeApi.remove).not.toHaveBeenCalled();
    options.accept();
    expect(storeApi.remove).toHaveBeenCalledWith('TTI', 'p2');
  });

  it('applies the built-in local model when the last saved profile is deleted', async () => {
    vi.stubGlobal('useConfirm', () => ({ require: (o?: { accept?: () => void }) => o?.accept?.() }));
    const KEYS = ['EMBEDDING_MODEL_LOCAL', 'EMBEDDING_API_NAME'];
    const VALUES: Record<string, string> = {
      EMBEDDING_MODEL_LOCAL: 'false',
      EMBEDDING_API_NAME: 'bge-m3'
    };
    makeStore([{ id: 'e1', label: 'only', params: { ...VALUES } }], 'e1', 'EMBEDDING');
    const wrapper = mountPanel('EMBEDDING', KEYS, VALUES);
    await expand(wrapper);
    await rowFor(wrapper, 'bge-m3')!.trigger('click');
    await buttonFor(wrapper, '删除')!.trigger('click');

    expect(storeApi.remove).toHaveBeenCalledWith('EMBEDDING', 'e1');
    expect(wrapper.emitted('apply')![0]![0]).toEqual({
      id: 'builtin:local',
      params: { EMBEDDING_MODEL_LOCAL: 'true' }
    });
  });

  it('offers no delete button for the built-in local entry', async () => {
    const KEYS = ['EMBEDDING_MODEL_LOCAL', 'EMBEDDING_API_NAME'];
    const VALUES: Record<string, string> = {
      EMBEDDING_MODEL_LOCAL: 'true',
      EMBEDDING_API_NAME: 'bge-m3'
    };
    makeStore([{ id: 'e1', label: 'emb', params: { ...VALUES } }], 'e1', 'EMBEDDING');
    const wrapper = mountPanel('EMBEDDING', KEYS, VALUES);
    await expand(wrapper);
    await rowFor(wrapper, '本地模型')!.trigger('click');
    expect(buttonFor(wrapper, '删除')).toBeUndefined();
  });

  it('renders no local entry for a group without a local flag', async () => {
    makeStore([{ id: 't1', label: 'tti', params: { TTI_API_NAME: 'tti' } }], 't1', 'TTI');
    const wrapper = mountPanel('TTI', ['TTI_MODEL_PROVIDER', 'TTI_API_NAME'], {
      TTI_MODEL_PROVIDER: 'openai',
      TTI_API_NAME: 'tti'
    });
    await expand(wrapper);
    expect(rowFor(wrapper, '本地模型')).toBeUndefined();
    expect(rowFor(wrapper, 'tti')).toBeTruthy();
  });

  it('warns about the 128K floor on MAIN_LLM_MAX_TOKEN, and only there', async () => {
    const KEYS = ['MAIN_LLM_PROVIDER', 'MAIN_LLM_MAX_TOKEN', 'REASONER_LLM_MAX_TOKEN'];
    const VALUES: Record<string, string> = {
      MAIN_LLM_PROVIDER: 'deepseek',
      MAIN_LLM_MAX_TOKEN: '131072',
      REASONER_LLM_MAX_TOKEN: '100000'
    };
    makeStore([{ id: 'p1', label: 'm', params: { ...VALUES } }], 'p1');
    const wrapper = mountPanel('MAIN_LLM', KEYS, VALUES);
    await expand(wrapper);

    // One guarded key in this panel → exactly one hint, and the free
    // REASONER_LLM_MAX_TOKEN (deliberately below 128K) must not carry it.
    expect(countHints(wrapper)).toBe(1);
    const tokenBlock = wrapper
      .findAll('div')
      .find(d => d.find('span').exists() && d.find('span').text() === 'MAIN_LLM_MAX_TOKEN');
    expect(tokenBlock?.text()).toContain('必须 >= 131072 (128K)');
    const freeBlock = wrapper
      .findAll('div')
      .find(d => d.find('span').exists() && d.find('span').text() === 'REASONER_LLM_MAX_TOKEN');
    expect(freeBlock?.text()).not.toContain('131072 (128K)');
  });

  it('hides the 128K warning for the built-in local entry (its budget is unused)', async () => {
    const KEYS = ['AUXILIARY_LLM_MODEL_LOCAL', 'AUXILIARY_LLM_MAX_TOKEN'];
    const VALUES: Record<string, string> = {
      AUXILIARY_LLM_MODEL_LOCAL: 'true',
      AUXILIARY_LLM_MAX_TOKEN: '131072'
    };
    makeStore([{ id: 'a1', label: 'aux', params: { ...VALUES } }], 'a1', 'AUXILIARY_LLM');
    const wrapper = mountPanel('AUXILIARY_LLM', KEYS, VALUES);
    await expand(wrapper);
    expect(countHints(wrapper)).toBe(1);

    await rowFor(wrapper, '本地模型')!.trigger('click');
    expect(countHints(wrapper)).toBe(0);
  });
});
