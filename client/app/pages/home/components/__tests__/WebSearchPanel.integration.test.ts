import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount } from '@vue/test-utils';
import WebSearchPanel from '@/pages/home/components/WebSearchPanel.vue';

// The store is stubbed so the panel is exercised through its observable
// surface (what it renders and what it writes) without a backend. Only the
// WRITE side of the env bridge is mocked: the panel reads the .env view from
// the search state payload (those keys are withheld from GET /env).
let storeApi: {
  stateFor: ReturnType<typeof vi.fn>;
  select: ReturnType<typeof vi.fn>;
  hydrate: ReturnType<typeof vi.fn>;
};
let envApi: { writeEnvConfig: ReturnType<typeof vi.fn> };

vi.mock('@/composables/env', () => ({
  writeEnvConfig: (...args: unknown[]) => envApi.writeEnvConfig(...args)
}));

/** Route double: the panel resolves the active session from it. */
const routeState = vi.hoisted(() => ({ sid: 'sid-1' }));
vi.mock('vue-router', () => ({
  useRoute: () => ({ params: { sid: routeState.sid }, path: `/home/${routeState.sid}`, query: {} })
}));

/**
 * The wire shape the panel consumes (`GET /sessions/search_backend`).
 * @param overrides
 */
function backendState(overrides: Record<string, unknown> = {}) {
  return {
    backend: null,
    effective: 'tavily',
    pending: false,
    pending_value: null,
    tavily_available: true,
    searxng: {
      configured: true,
      url: 'http://localhost:8888',
      engines: '',
      categories: '',
      proxy: '',
      verify_tls: true
    },
    env: {
      searxng_url: 'http://localhost:8888',
      searxng_proxy: '',
      searxng_engines: '',
      searxng_categories: '',
      searxng_verify_tls: true,
      tavily_key_present: true
    },
    ...overrides
  };
}

const stubs = {
  InputText: {
    props: ['modelValue', 'placeholder', 'type'],
    emits: ['update:modelValue'],
    template: '<input :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />'
  },
  Select: {
    props: ['modelValue', 'options', 'optionLabel', 'optionValue'],
    emits: ['update:modelValue'],
    template: '<div class="select-stub" />'
  },
  ToggleSwitch: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<div class="switch-stub" />'
  },
  Button: {
    props: ['label', 'disabled'],
    emits: ['click'],
    template: '<button class="btn-stub" :disabled="disabled" @click="$emit(\'click\', $event)">{{ label }}</button>'
  }
};

/**
 * Mount the panel for one session with the given backend/env state.
 * @param overrides Backend-state overrides.
 * @param env Overrides for the state payload's `env` (file) block.
 * @param sessionId The open session (`''` = none).
 */
async function mountPanel(
  overrides: Record<string, unknown> = {},
  env: Record<string, unknown> = {},
  sessionId = 'sid-1'
) {
  routeState.sid = sessionId;
  const base = backendState(overrides);
  const payload = { ...base, env: { ...base.env, ...env } };
  storeApi = {
    stateFor: vi.fn(() => payload),
    select: vi.fn(),
    hydrate: vi.fn()
  };
  vi.stubGlobal(
    'useWebSearchStore',
    vi.fn(() => storeApi)
  );
  envApi = { writeEnvConfig: vi.fn(async () => true) };
  const wrapper = mount(WebSearchPanel, { global: { stubs } });
  await wrapper.vm.$nextTick();
  return wrapper;
}

describe('WebSearchPanel.vue (integration, store + env mocked)', () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it('renders the backend choice, the Tavily card and the SearXNG form', async () => {
    const wrapper = await mountPanel();

    expect(wrapper.find('[data-test="web-search-backend-tavily"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="web-search-backend-searxng"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="web-search-tavily-status"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="web-search-none-configured"]').exists()).toBe(false);
    // A configured instance opens its own form (the URL is the reason to look).
    expect(wrapper.find('[data-test="web-search-searxng-form"]').exists()).toBe(true);
  });

  it('fills the SearXNG form from the state payload', async () => {
    const wrapper = await mountPanel({}, { searxng_url: 'http://from-file:9999', searxng_engines: 'google' });

    expect((wrapper.find('[data-test="web-search-searxng-url"]').element as HTMLInputElement).value).toBe(
      'http://from-file:9999'
    );
    expect((wrapper.find('[data-test="web-search-searxng-engines"]').element as HTMLInputElement).value).toBe('google');
  });

  it('tells neither-provider-configured apart from a disabled row', async () => {
    const wrapper = await mountPanel({
      tavily_available: false,
      searxng: { configured: false, url: '', engines: '', categories: '', proxy: '', verify_tls: true }
    });

    expect(wrapper.find('[data-test="web-search-none-configured"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="web-search-backend-tavily"]').exists()).toBe(false);
  });

  it('shows only the unconfigured row as disabled when one provider exists', async () => {
    const wrapper = await mountPanel({ tavily_available: false });

    const tavily = wrapper.find('[data-test="web-search-backend-tavily"]');
    const searxng = wrapper.find('[data-test="web-search-backend-searxng"]');
    expect(tavily.attributes('disabled')).toBeDefined();
    expect(searxng.attributes('disabled')).toBeUndefined();
  });

  it('select() receives the clicked backend', async () => {
    const wrapper = await mountPanel();

    await wrapper.find('[data-test="web-search-backend-searxng"]').trigger('click');

    expect(storeApi.select).toHaveBeenCalledWith('sid-1', 'searxng');
  });

  it('offers "follow the default" only when a choice is set, clearing it to null', async () => {
    const wrapper = await mountPanel({ backend: 'searxng', effective: 'searxng' });

    await wrapper.find('[data-test="web-search-follow-default"]').trigger('click');

    expect(storeApi.select).toHaveBeenCalledWith('sid-1', null);
  });

  it('annotates a downgrade and a parked choice', async () => {
    const wrapper = await mountPanel({ backend: 'tavily', effective: 'searxng', pending: true });

    expect(wrapper.find('[data-test="web-search-degraded"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="web-search-pending"]').exists()).toBe(true);
  });

  it('without an open session the panel says so and refuses to switch', async () => {
    const wrapper = await mountPanel({}, {}, '');

    expect(wrapper.find('[data-test="web-search-no-session"]').exists()).toBe(true);
    // Availability still renders (the sections are global), but the rows are inert.
    expect(wrapper.find('[data-test="web-search-backend-tavily"]').attributes('disabled')).toBeDefined();
  });

  it('shows the three Tavily states (absent / saved / live)', async () => {
    const absent = await mountPanel({ tavily_available: false }, { tavily_key_present: false });
    expect(absent.find('[data-test="web-search-tavily-status"]').text()).toContain('未配置');

    const saved = await mountPanel({ tavily_available: false }, { tavily_key_present: true });
    expect(saved.find('[data-test="web-search-tavily-status"]').text()).toContain('重启后端后生效');

    const live = await mountPanel();
    expect(live.find('[data-test="web-search-tavily-status"]').text()).toContain('已生效');
  });

  it('writes the drafted Tavily key (never reading one back)', async () => {
    const wrapper = await mountPanel();

    // Nothing to save yet: the button is inert (an empty key cannot be written).
    expect(wrapper.find('[data-test="web-search-tavily-save"]').attributes('disabled')).toBeDefined();

    await wrapper.find('[data-test="web-search-tavily-key"]').setValue('tvly-draft');
    await wrapper.find('[data-test="web-search-tavily-save"]').trigger('click');
    await vi.waitFor(() => expect(envApi.writeEnvConfig).toHaveBeenCalled());

    expect(envApi.writeEnvConfig).toHaveBeenCalledWith({ TAVILY_API_KEY: 'tvly-draft' });
    // The value is write-only: the field clears after a successful save.
    await vi.waitFor(() => expect(wrapper.find('[data-test="web-search-tavily-saved"]').exists()).toBe(true));
  });

  it('clears the stored Tavily key', async () => {
    const wrapper = await mountPanel();

    await wrapper.find('[data-test="web-search-tavily-clear"]').trigger('click');
    await vi.waitFor(() => expect(envApi.writeEnvConfig).toHaveBeenCalled());

    expect(envApi.writeEnvConfig).toHaveBeenCalledWith({ TAVILY_API_KEY: '' });
  });

  it('saves only the changed SearXNG keys and shows the restart hint', async () => {
    const wrapper = await mountPanel();

    // The loaded value matches the runtime one → no restart owed yet.
    expect(wrapper.find('[data-test="web-search-restart-hint"]').exists()).toBe(false);

    await wrapper.find('[data-test="web-search-searxng-url"]').setValue('http://localhost:9999');
    expect(wrapper.find('[data-test="web-search-restart-hint"]').exists()).toBe(true);

    await wrapper.find('[data-test="web-search-searxng-save"]').trigger('click');
    await vi.waitFor(() => expect(envApi.writeEnvConfig).toHaveBeenCalled());

    expect(envApi.writeEnvConfig).toHaveBeenCalledWith({ SEARXNG_URL: 'http://localhost:9999' });
    await vi.waitFor(() => expect(wrapper.find('[data-test="web-search-saved"]').exists()).toBe(true));
  });

  it('reports a refused save', async () => {
    const wrapper = await mountPanel();
    envApi.writeEnvConfig.mockResolvedValue(false);

    await wrapper.find('[data-test="web-search-searxng-proxy"]').setValue('http://127.0.0.1:7890');
    await wrapper.find('[data-test="web-search-searxng-save"]').trigger('click');

    await vi.waitFor(() => expect(wrapper.find('[data-test="web-search-save-failed"]').exists()).toBe(true));
  });
});
