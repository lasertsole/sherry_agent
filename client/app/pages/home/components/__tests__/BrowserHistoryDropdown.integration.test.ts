/**
 * The address bar's history dropdown (integration).
 *
 * Contract: it lists the matched visits with their age (localized) and a
 * "this session" mark, picking one emits `select` with its URL, the empty
 * state shows when there is nothing, and the clear action wipes the store.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import BrowserHistoryDropdown from '@/pages/home/components/BrowserHistoryDropdown.vue';
import type { BrowserVisit } from '@/composables/db';

const history = vi.hoisted(() => ({
  searchBrowserVisits: vi.fn(async () => [] as BrowserVisit[]),
  clearBrowserVisits: vi.fn(async () => undefined),
  visitAge: vi.fn(() => ({ unit: 'minutes' as const, value: 3 }))
}));
vi.mock('~/composables/use-browser-history', () => history);

function visit(overrides: Partial<BrowserVisit> = {}): BrowserVisit {
  return {
    id: 1,
    sessionId: 'sid-1',
    url: 'https://docs.python.org/3/library/asyncio.html',
    title: 'asyncio — Asynchronous I/O',
    visitedAt: Date.now() - 180_000,
    ...overrides
  };
}

/**
 * Mount the dropdown with sane defaults.
 * @param props
 */
function mountDropdown(props: Record<string, unknown> = {}) {
  return mount(BrowserHistoryDropdown, {
    props: { query: '', open: true, sessionId: 'sid-1', ...props },
    global: { stubs: { ProgressSpinner: true } }
  });
}

describe('BrowserHistoryDropdown', () => {
  beforeEach(() => {
    history.searchBrowserVisits.mockReset().mockResolvedValue([]);
    history.clearBrowserVisits.mockReset().mockResolvedValue(undefined);
    history.visitAge.mockReset().mockReturnValue({ unit: 'minutes', value: 3 });
  });

  it('renders the visits with URL, title and the localized age', async () => {
    history.searchBrowserVisits.mockResolvedValue([visit()]);
    const wrapper = mountDropdown();
    await flushPromises();

    expect(wrapper.find('[data-test="browser-history-dropdown"]').exists()).toBe(true);
    expect(wrapper.text()).toContain('https://docs.python.org/3/library/asyncio.html');
    expect(wrapper.text()).toContain('asyncio — Asynchronous I/O');
    expect(wrapper.text()).toContain('3 分钟前');
    expect(wrapper.text()).toContain('当前会话');
  });

  it('marks visits from another session without the "this session" note', async () => {
    history.searchBrowserVisits.mockResolvedValue([visit({ sessionId: 'sid-2' })]);
    const wrapper = mountDropdown();
    await flushPromises();

    expect(wrapper.text()).not.toContain('当前会话');
  });

  it('emits select with the URL on mousedown (before the input blurs)', async () => {
    history.searchBrowserVisits.mockResolvedValue([visit()]);
    const wrapper = mountDropdown();
    await flushPromises();

    await wrapper.get('[data-test="browser-history-item-1"]').trigger('mousedown');

    expect(wrapper.emitted('select')?.[0]).toEqual(['https://docs.python.org/3/library/asyncio.html']);
  });

  it('shows the empty state when there is nothing to offer', async () => {
    const wrapper = mountDropdown();
    await flushPromises();

    expect(wrapper.find('[data-test="browser-history-empty"]').text()).toBe('暂无历史记录');
    // Nothing to clear → no clear action either.
    expect(wrapper.find('[data-test="browser-history-clear"]').exists()).toBe(false);
  });

  it('hides itself when closed', async () => {
    history.searchBrowserVisits.mockResolvedValue([visit()]);
    const wrapper = mountDropdown({ open: false });
    await flushPromises();

    expect(wrapper.find('[data-test="browser-history-dropdown"]').exists()).toBe(false);
    expect(history.searchBrowserVisits).not.toHaveBeenCalled();
  });

  it('re-queries when the query changes', async () => {
    history.searchBrowserVisits.mockResolvedValue([visit()]);
    const wrapper = mountDropdown({ query: 'py' });
    await flushPromises();
    expect(history.searchBrowserVisits).toHaveBeenCalledWith('py');

    await wrapper.setProps({ query: 'python' });
    await flushPromises();
    expect(history.searchBrowserVisits).toHaveBeenLastCalledWith('python');
  });

  it('clears the history through the clear action and refreshes the list', async () => {
    history.searchBrowserVisits.mockResolvedValueOnce([visit()]).mockResolvedValueOnce([]);
    const wrapper = mountDropdown();
    await flushPromises();

    await wrapper.get('[data-test="browser-history-clear"]').trigger('mousedown');
    await flushPromises();

    expect(history.clearBrowserVisits).toHaveBeenCalledTimes(1);
    expect(history.searchBrowserVisits).toHaveBeenCalledTimes(2);
    expect(wrapper.find('[data-test="browser-history-empty"]').exists()).toBe(true);
  });

  it('hides a favicon that fails to load, keeping the globe glyph', async () => {
    history.searchBrowserVisits.mockResolvedValue([visit()]);
    const wrapper = mountDropdown();
    await flushPromises();

    const img = wrapper.get('img');
    expect(img.attributes('src')).toBe('https://docs.python.org/favicon.ico');
    await img.trigger('error');
    await wrapper.vm.$nextTick();

    expect(wrapper.find('img').exists()).toBe(false);
  });
});
