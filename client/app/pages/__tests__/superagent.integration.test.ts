/**
 * The /superagent page: it polls the superagent loopback API and renders the
 * six panel tabs. The test stubs fetch so no side process is needed.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import SuperagentPage from '@/pages/superagent/index.vue';

const fetchMock = vi.fn(async () => ({
  ok: true,
  status: 200,
  json: async () => ({
    needs_attention: ['ticket-1'],
    paused: false,
    load: { running: 1, queued: 0, maxWorkers: 2 },
    masterId: 'superagent-main'
  })
}));

describe('superagent page', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', fetchMock);
    vi.stubGlobal('useRuntimeConfig', () => ({ public: { superagentUrl: 'http://127.0.0.1:3082' } }));
    fetchMock.mockClear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('polls state and exposes all six tabs', async () => {
    const wrapper = mount(SuperagentPage);
    await flushPromises();
    expect(fetchMock).toHaveBeenCalledWith(
      'http://127.0.0.1:3082/state',
      expect.objectContaining({ headers: { 'x-orchestrator-request': '1' } })
    );
    expect(wrapper.findAll('[data-test^="superagent-tab-"]')).toHaveLength(6);
    // The vitest i18n stub resolves against the real zh messages.
    expect(wrapper.get('[data-test="superagent-status"]').text()).toContain('运行正常');
    expect(wrapper.get('[data-test="superagent-panel"]').text()).toContain('ticket-1');
    wrapper.unmount();
  });
});
