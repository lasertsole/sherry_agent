/**
 * System Config → 模型配置: the structured-output degradation panel.
 *
 * The list is read-only from the backend's perspective; clearing a row forces
 * the next spawn to retry the default structured-output strategy. The test
 * drives the real bridge over a mocked transport, pinning the load/render, both
 * clear paths, and the fail-open copy.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import ModelCapsSection from '@/pages/home/components/ModelCapsSection.vue';

const api = vi.hoisted(() => ({ fetchApi: vi.fn() }));

vi.mock('@/composables/requestApi', () => ({ fetchApi: api.fetchApi }));

const ENTRY = {
  provider: 'deepseek',
  model: 'deepseek-flash',
  level: 2,
  effective_level: 1,
  reason: '400 tool_choice',
  escalated_at: '',
  updated_at: '',
  reprobe_count: 0
};

const ButtonStub = {
  name: 'Button',
  props: ['label', 'icon', 'disabled', 'loading', 'severity', 'text', 'size'],
  emits: ['click'],
  template: '<button :disabled="disabled" @click="$emit(\'click\')"><slot /></button>'
};

const mountSection = () => mount(ModelCapsSection, { global: { stubs: { Button: ButtonStub } } });

beforeEach(() => {
  api.fetchApi.mockReset().mockImplementation(({ method }: { method?: string }) => {
    if (method === 'delete') return Promise.resolve({ success: true });
    return Promise.resolve({ entries: [] });
  });
});

describe('ModelCapsSection', () => {
  it('renders the persisted degradations with both levels', async () => {
    api.fetchApi.mockResolvedValueOnce({ entries: [ENTRY] });

    const wrapper = mountSection();
    await flushPromises();

    const rows = wrapper.findAll('[data-test="model-caps-row"]');
    expect(rows).toHaveLength(1);
    expect(rows[0].text()).toContain('deepseek/deepseek-flash');
    expect(rows[0].text()).toContain('持久级别 2');
    expect(rows[0].text()).toContain('下次使用 1');
    expect(rows[0].text()).toContain('400 tool_choice');
  });

  it('shows the empty state when nothing degraded', async () => {
    const wrapper = mountSection();
    await flushPromises();

    expect(wrapper.text()).toContain('暂无降级模型。');
  });

  it('clears one entry and reloads the list', async () => {
    api.fetchApi.mockResolvedValueOnce({ entries: [ENTRY] });
    const wrapper = mountSection();
    await flushPromises();

    await wrapper.find('[data-test="model-caps-clear-one"]').trigger('click');
    await flushPromises();

    expect(api.fetchApi).toHaveBeenCalledWith(
      expect.objectContaining({
        method: 'delete',
        url: expect.stringContaining('provider=deepseek')
      })
    );
    expect(api.fetchApi).toHaveBeenCalledTimes(3);
  });

  it('clears every entry', async () => {
    api.fetchApi.mockResolvedValueOnce({ entries: [ENTRY] });
    const wrapper = mountSection();
    await flushPromises();

    await wrapper.find('[data-test="model-caps-clear-all"]').trigger('click');
    await flushPromises();

    expect(api.fetchApi).toHaveBeenCalledWith({
      url: '/sherry-config/model-caps',
      method: 'delete'
    });
    expect(api.fetchApi).toHaveBeenCalledTimes(3);
  });

  it('surfaces a load failure instead of rendering a stale list', async () => {
    api.fetchApi.mockResolvedValueOnce(null);

    const wrapper = mountSection();
    await flushPromises();

    expect(wrapper.text()).toContain('降级记录读取失败。');
  });
});
