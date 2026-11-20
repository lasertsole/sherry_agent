/**
 * 高危防护 panel: keyword registry + executed-operation ledger.
 *
 * The panel is a thin view over one snapshot endpoint; every mutation routes
 * through the same `fetchApi` seam the real bridge helpers use.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';

import HighRiskGuardPanel from '../HighRiskGuardPanel.vue';

const api = vi.hoisted(() => ({ fetchApi: vi.fn() }));

vi.mock('@/composables/requestApi', () => ({ fetchApi: api.fetchApi }));

const KEYWORDS = [
  {
    id: null,
    keyword: 'rm -rf',
    policy: 'block',
    source: 'builtin',
    executed: false,
    result_summary: '',
    scope: 'builtin'
  },
  {
    id: 7,
    keyword: 'deploy_prod',
    policy: 'block',
    source: 'user',
    executed: true,
    result_summary: 'deployed to prod',
    scope: 'sess-1'
  }
];

const OPS = [
  {
    id: 3,
    tool_name: 'mcp__stripe__charge',
    args_preview: '{"amount": 100}',
    result_summary: 'charged',
    executed_at: 1_700_000_000
  }
];

type Snapshot = { keywords: typeof KEYWORDS; ops: typeof OPS };

const routes: { snapshot: Snapshot; writeOk: boolean; message: string } = {
  snapshot: { keywords: KEYWORDS, ops: OPS },
  writeOk: true,
  message: ''
};

beforeEach(() => {
  routes.snapshot = { keywords: KEYWORDS.map(row => ({ ...row })), ops: OPS.map(row => ({ ...row })) };
  routes.writeOk = true;
  routes.message = '';
  api.fetchApi.mockReset().mockImplementation(async (params: { url: string; method?: string }) => {
    const url = String(params.url);
    if (params.method === 'get') return routes.snapshot;
    if (params.method === 'delete') return { success: true };
    if (!routes.writeOk) return { success: false, message: routes.message };
    return { success: true };
  });
});

const stubs = {
  Button: {
    name: 'Button',
    props: ['label', 'disabled', 'loading', 'icon', 'severity', 'text', 'size'],
    emits: ['click'],
    template: '<button :disabled="disabled" @click="$emit(\'click\')">{{ label }}<slot /></button>'
  },
  InputText: {
    name: 'InputText',
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template:
      '<input :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />'
  },
  Tag: { name: 'Tag', props: ['value', 'severity'], template: '<span class="tag">{{ value }}</span>' },
  Divider: { name: 'Divider', template: '<hr />' }
};

function mountPanel() {
  return mount(HighRiskGuardPanel, {
    props: { payload: { sessionId: 'sess-1' } },
    global: { stubs }
  });
}

function callsMatching(method: string, fragment: string) {
  return api.fetchApi.mock.calls.filter(
    ([params]) => params.method === method && String(params.url).includes(fragment)
  );
}

describe('HighRiskGuardPanel', () => {
  it('loads and renders keywords and ledger entries', async () => {
    const wrapper = mountPanel();
    await flushPromises();

    expect(String(api.fetchApi.mock.calls[0]?.[0]?.url)).toBe(
      '/sessions/sess-1/high-risk-keywords'
    );
    expect(wrapper.text()).toContain('rm -rf');
    expect(wrapper.text()).toContain('deploy_prod');
    expect(wrapper.text()).toContain('mcp__stripe__charge');
    expect(wrapper.text()).toContain('charged');
  });

  it('adds a keyword and reloads the snapshot', async () => {
    const wrapper = mountPanel();
    await flushPromises();

    const input = wrapper.find('input');
    await input.setValue('deploy_prod2');
    const addButton = wrapper.findAll('button').find(button => button.text() === '添加');
    expect(addButton).toBeTruthy();
    await addButton!.trigger('click');
    await flushPromises();

    const post = callsMatching('post', '/high-risk-keywords');
    expect(post.length).toBe(1);
    expect(post[0]![0].opts).toEqual({ keyword: 'deploy_prod2', scope: 'session' });
    // One initial load + one reload after the mutation.
    expect(callsMatching('get', '/high-risk-keywords').length).toBe(2);
  });

  it('keeps the add button disabled for a short keyword', async () => {
    const wrapper = mountPanel();
    await flushPromises();

    await wrapper.find('input').setValue('ab');
    const addButton = wrapper.findAll('button').find(button => button.text() === '添加');
    expect(addButton?.attributes('disabled')).toBeDefined();
    expect(wrapper.text()).toContain('至少 3 个字符');
  });

  it('shows the backend refusal message when a write fails', async () => {
    routes.writeOk = false;
    routes.message = '关键字长度不能小于 3 个字符';
    const wrapper = mountPanel();
    await flushPromises();

    await wrapper.find('input').setValue('abcd');
    const addButton = wrapper.findAll('button').find(button => button.text() === '添加');
    await addButton!.trigger('click');
    await flushPromises();

    expect(wrapper.find('[data-test="high-risk-error"]').text()).toContain('3 个字符');
  });

  it('switches a keyword policy through PUT', async () => {
    const wrapper = mountPanel();
    await flushPromises();

    const allow = wrapper.findAll('button').find(button => button.text() === '改为放行');
    await allow!.trigger('click');
    await flushPromises();

    const put = callsMatching('put', '/high-risk-keywords');
    expect(put.length).toBe(1);
    expect(put[0]![0].opts).toEqual({ keyword: 'rm -rf', policy: 'allow' });
  });

  it('resets an executed marker and deletes a custom keyword', async () => {
    const wrapper = mountPanel();
    await flushPromises();

    const reset = wrapper.findAll('button').find(button => button.text() === '重置执行标记');
    expect(reset).toBeTruthy();
    await reset!.trigger('click');
    await flushPromises();
    const resets = callsMatching('post', '/high-risk-keywords/reset');
    expect(resets.length).toBe(1);
    expect(resets[0]![0].opts).toEqual({ keyword: 'deploy_prod' });

    const keywordRow = wrapper.find('[data-test="high-risk-keyword-deploy_prod"]');
    const trash = keywordRow.findAll('button').find(button => button.attributes('aria-label') === '删除');
    await trash!.trigger('click');
    await flushPromises();
    expect(callsMatching('delete', '/high-risk-keywords/7').length).toBe(1);
  });

  it('deletes a ledger row', async () => {
    const wrapper = mountPanel();
    await flushPromises();

    const opRow = wrapper.find('[data-test="high-risk-op-3"]');
    const trash = opRow.findAll('button').find(button => button.attributes('aria-label') === '删除');
    await trash!.trigger('click');
    await flushPromises();
    expect(callsMatching('delete', '/high-risk-ops/3').length).toBe(1);
  });

  it('shows the no-session hint when opened without a session', async () => {
    const wrapper = mount(HighRiskGuardPanel, {
      props: { payload: {} },
      global: { stubs }
    });
    await flushPromises();
    expect(wrapper.text()).toContain('请先打开一个会话');
    expect(api.fetchApi).not.toHaveBeenCalled();
  });
});
