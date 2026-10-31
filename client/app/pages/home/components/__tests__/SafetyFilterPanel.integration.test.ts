/**
 * 系统配置 → 直播安全: the master switch, the LLM review (off without a model),
 * the keyword-rules editor and the recent-events list.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';

import SafetyFilterPanel from '../SafetyFilterPanel.vue';

const api = vi.hoisted(() => ({ fetchApi: vi.fn(), toastSuccess: vi.fn() }));

// One seam for the whole tab: every backend call (sherry.jsonc switches, the
// rules file, the test scan, the log) goes through fetchApi, so the mock routes
// by URL and the real bridge helpers run.
vi.mock('@/composables/requestApi', () => ({ fetchApi: api.fetchApi }));
vi.mock('@/composables/toast', () => ({ toastSuccess: api.toastSuccess }));

const SAFETY_ENTRIES = [
  { key: 'safety_filter.enabled', value: 'false', value_edited: false },
  { key: 'safety_filter.llm_moderation_mode', value: 'off', value_edited: false },
  { key: 'safety_filter.llm_moderation_model', value: '', value_edited: false }
];

/** What each endpoint answers this run (tests override before mounting). */
const routes = {
  entries: SAFETY_ENTRIES,
  count: 1,
  hits: [] as Array<Record<string, unknown>>,
  events: [] as Array<Record<string, unknown>>,
  rulesWriteOk: true,
  rulesWriteMessage: ''
};

beforeEach(() => {
  routes.entries = SAFETY_ENTRIES.map(entry => ({ ...entry }));
  routes.count = 1;
  routes.hits = [];
  routes.events = [];
  routes.rulesWriteOk = true;
  routes.rulesWriteMessage = '';
  api.toastSuccess.mockReset();
  api.fetchApi.mockReset().mockImplementation(async (params: { url: string; method?: string }) => {
    const url = String(params.url);
    if (url.startsWith('/sherry-config')) return { entries: routes.entries };
    if (url.startsWith('/safety-filter/rules')) {
      if (params.method === 'put') {
        return routes.rulesWriteOk
          ? { success: true, count: routes.count }
          : { success: false, message: routes.rulesWriteMessage };
      }
      return {
        path: '/tmp/workspace/sensitive_words.json',
        exists: true,
        count: routes.count,
        rules: [{ pattern: 'secret', type: 'contains', action: 'block', scope: 'both', replacement: '' }]
      };
    }
    if (url.startsWith('/safety-filter/test')) return { hits: routes.hits };
    if (url.startsWith('/safety-filter/log')) return { events: routes.events };
    return null;
  });
});

/** The sherry.jsonc write the panel issued last (`opts.changes` map). */
const lastSherryChange = (): Record<string, string> => {
  const call = api.fetchApi.mock.calls
    .filter(([params]) => String(params.url).startsWith('/sherry-config') && params.method === 'put')
    .pop();
  return (call?.[0]?.opts as { changes?: Record<string, string> } | undefined)?.changes ?? {};
};

const stubs = {
  ToggleSwitch: {
    name: 'ToggleSwitch',
    props: ['modelValue', 'disabled'],
    emits: ['update:modelValue'],
    template:
      '<input type="checkbox" class="switch" :checked="modelValue" :disabled="disabled" @change="$emit(\'update:modelValue\', $event.target.checked)" />'
  },
  InputText: {
    name: 'InputText',
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<input class="it" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />'
  },
  Select: {
    name: 'Select',
    props: ['modelValue', 'options', 'disabled'],
    emits: ['update:modelValue'],
    template:
      '<select class="select" :value="modelValue" :disabled="disabled"><option v-for="option in options" :key="option.value" :value="option.value">{{ option.label }}</option></select>'
  },
  Textarea: {
    name: 'Textarea',
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<textarea class="ta" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />'
  },
  Button: {
    name: 'Button',
    props: ['label', 'disabled'],
    emits: ['click'],
    template: '<button class="btn" :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>'
  }
};

/** Mount the panel and wait for all three loads. */
async function mountPanel() {
  const wrapper = mount(SafetyFilterPanel, { global: { stubs } });
  await flushPromises();
  return wrapper;
}

describe('SafetyFilterPanel', () => {
  it('loads the switches, the rules and the log on mount', async () => {
    const wrapper = await mountPanel();

    expect(api.fetchApi).toHaveBeenCalledWith(expect.objectContaining({ url: '/safety-filter/rules' }));
    expect(api.fetchApi).toHaveBeenCalledWith(
      expect.objectContaining({ url: expect.stringContaining('/safety-filter/log') })
    );
    expect(wrapper.text()).toContain('已加载 1 条规则');
    expect((wrapper.find('[data-test="safety-rules-editor"]').element as HTMLTextAreaElement).value).toContain(
      '"secret"'
    );
    expect((wrapper.find('[data-test="safety-master-switch"]').element as HTMLInputElement).checked).toBe(false);
  });

  it('persists the master switch as a sherry.jsonc key', async () => {
    const wrapper = await mountPanel();

    // `trigger` does not flip a checkbox: set the native state, then fire change.
    (wrapper.find('[data-test="safety-master-switch"]').element as HTMLInputElement).checked = true;
    await wrapper.find('[data-test="safety-master-switch"]').trigger('change');
    await flushPromises();

    expect(lastSherryChange()).toMatchObject({ 'safety_filter.enabled': 'true' });
  });

  it('keeps the LLM review unusable until a review model is configured', async () => {
    const wrapper = await mountPanel();
    expect((wrapper.find('[data-test="safety-llm-switch"]').element as HTMLInputElement).disabled).toBe(true);
    expect(wrapper.text()).toContain('未配置审核模型');

    await wrapper.find('[data-test="safety-llm-model"]').setValue('judge-model');
    await wrapper.find('[data-test="safety-llm-model"]').trigger('change');
    await flushPromises();

    expect((wrapper.find('[data-test="safety-llm-switch"]').element as HTMLInputElement).disabled).toBe(false);
    expect(lastSherryChange()).toMatchObject({ 'safety_filter.llm_moderation_model': 'judge-model' });
  });

  it('writes the mode only while the LLM switch is on', async () => {
    routes.entries = [
      { key: 'safety_filter.enabled', value: 'true', value_edited: false },
      { key: 'safety_filter.llm_moderation_mode', value: 'always', value_edited: false },
      { key: 'safety_filter.llm_moderation_model', value: 'judge', value_edited: false }
    ];
    const wrapper = await mountPanel();

    (wrapper.find('[data-test="safety-llm-switch"]').element as HTMLInputElement).checked = false;
    await wrapper.find('[data-test="safety-llm-switch"]').trigger('change');
    await flushPromises();

    expect(lastSherryChange()).toMatchObject({ 'safety_filter.llm_moderation_mode': 'off' });
  });

  it('saves the rules through the validated endpoint', async () => {
    const wrapper = await mountPanel();

    await wrapper.find('[data-test="safety-rules-save"]').trigger('click');
    await flushPromises();

    const put = api.fetchApi.mock.calls.find(
      ([params]) => String(params.url) === '/safety-filter/rules' && params.method === 'put'
    );
    expect(put?.[0]?.opts).toEqual({
      rules: [{ pattern: 'secret', type: 'contains', action: 'block', scope: 'both', replacement: '' }]
    });
    expect(api.toastSuccess).toHaveBeenCalled();
  });

  it('refuses a broken rules document without calling the backend', async () => {
    const wrapper = await mountPanel();

    const before = api.fetchApi.mock.calls.length;
    await wrapper.find('[data-test="safety-rules-editor"]').setValue('{not json');
    await wrapper.find('[data-test="safety-rules-save"]').trigger('click');
    await flushPromises();

    const writes = api.fetchApi.mock.calls
      .slice(before)
      .filter(([params]) => String(params.url) === '/safety-filter/rules' && params.method === 'put');
    expect(writes).toHaveLength(0);
    expect(wrapper.text()).toContain('JSON 格式不合法');
  });

  it('shows the backend\u2019s validation message when a rule is refused', async () => {
    routes.rulesWriteOk = false;
    routes.rulesWriteMessage = 'rule #1: invalid regex';
    const wrapper = await mountPanel();

    await wrapper.find('[data-test="safety-rules-save"]').trigger('click');
    await flushPromises();

    expect(wrapper.text()).toContain('invalid regex');
    expect(api.toastSuccess).not.toHaveBeenCalled();
  });

  it('lists the recent filter events', async () => {
    routes.events = [
      {
        at: 1_700_000_000,
        layer: 'input_keyword',
        action: 'block',
        rule_count: 2,
        message: '输入命中 2 条拦截规则，已拦截',
        snippet: ''
      }
    ];
    const wrapper = await mountPanel();

    expect(wrapper.find('[data-test="safety-log-list"]').text()).toContain('input_keyword');
    expect(wrapper.find('[data-test="safety-log-list"]').text()).toContain('已拦截');
  });

  it('shows the rules a sample text hits, and says when nothing matched', async () => {
    routes.hits = [{ pattern: 'secret', matched_text: 'secret', action: 'block', scope: 'both' }];
    const wrapper = await mountPanel();

    await wrapper.find('[data-test="safety-test-input"]').setValue('a secret');
    await wrapper.find('[data-test="safety-test-run"]').trigger('click');
    await flushPromises();
    expect(wrapper.find('[data-test="safety-test-hits"]').text()).toContain('secret');

    routes.hits = [];
    await wrapper.find('[data-test="safety-test-run"]').trigger('click');
    await flushPromises();
    expect(wrapper.find('[data-test="safety-test-empty"]').exists()).toBe(true);
  });
});
