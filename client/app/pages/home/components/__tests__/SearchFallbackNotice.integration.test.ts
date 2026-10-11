import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount } from '@vue/test-utils';
import SearchFallbackNotice from '@/pages/home/components/SearchFallbackNotice.vue';

// The store is a Nuxt auto-import; the banner only reads `fallbackFor` and calls
// `dismissFallback` / `subscribe`, so a hand-rolled double drives the surface.
let notice: { backend: string; reason: string; message: string; at: number } | null;
const storeApi = { fallbackFor: vi.fn(), dismissFallback: vi.fn(), subscribe: vi.fn() };

beforeEach(() => {
  notice = null;
  storeApi.fallbackFor.mockReset();
  storeApi.fallbackFor.mockImplementation(() => notice);
  storeApi.dismissFallback.mockReset();
  storeApi.subscribe.mockReset();
  vi.stubGlobal(
    'useWebSearchStore',
    vi.fn(() => storeApi)
  );
});

describe('SearchFallbackNotice.vue (integration, store mocked)', () => {
  it('renders nothing without a notice', () => {
    const wrapper = mount(SearchFallbackNotice, { props: { sessionId: 'sid-1' } });

    expect(wrapper.find('[data-test="search-fallback-banner"]').exists()).toBe(false);
    // The socket listener is registered regardless: the notice may arrive later.
    expect(storeApi.subscribe).toHaveBeenCalled();
  });

  it('renders the localized downgrade line with the reason translated', () => {
    notice = { backend: 'searxng', reason: 'tavily_auth_failed', message: 'raw', at: 1 };
    const wrapper = mount(SearchFallbackNotice, { props: { sessionId: 'sid-1' } });

    const banner = wrapper.find('[data-test="search-fallback-banner"]');
    expect(banner.exists()).toBe(true);
    expect(banner.text()).toContain('Tavily 无法使用（密钥无效或已过期），已切换到 SearXNG');
  });

  it('shows an unknown reason verbatim instead of swallowing it', () => {
    notice = { backend: 'searxng', reason: 'tavily_quota', message: 'raw', at: 1 };
    const wrapper = mount(SearchFallbackNotice, { props: { sessionId: 'sid-1' } });

    expect(wrapper.find('[data-test="search-fallback-banner"]').text()).toContain('tavily_quota');
  });

  it('dismisses through the store', async () => {
    notice = { backend: 'searxng', reason: 'tavily_timeout', message: 'raw', at: 1 };
    const wrapper = mount(SearchFallbackNotice, { props: { sessionId: 'sid-9' } });

    await wrapper.find('[data-test="search-fallback-dismiss"]').trigger('click');

    expect(storeApi.dismissFallback).toHaveBeenCalledWith('sid-9');
  });
});
