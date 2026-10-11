import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount } from '@vue/test-utils';
import WebSearchPanel from '@/pages/home/components/WebSearchPanel.vue';

// The store and the .env bridge are stubbed at their module/global seams, so the
// panel is exercised through the observable surface (what it renders and what it
// writes) without a backend.
let storeApi: {
  stateFor: ReturnType<typeof vi.fn>;
  select: ReturnType<typeof vi.fn>;
  hydrate: ReturnType<typeof vi.fn>;
};
let envApi: {
  readEnvConfig: ReturnType<typeof vi.fn>;
  writeEnvConfig: ReturnType<typeof vi.fn>;
};

vi.mock('@/composables/env', () => ({
  readEnvConfig: (...args: unknown[]) => envApi.readEnvConfig(...args),
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
    ...overrides
  };
}

/**
 * The .env payload the form loads from.
 * @param values
 */
function envPayload(values: Record<string, string>) {
  return {
    groups: [
      {
        name: 'SEARXNG',
        kind: 'plain',
        entries: Object.entries(values).map(([key, value]) => ({ key, value, value_edited: false }))
      }
    ]
  };
}

const stubs = {
  InputText: {
    props: ['modelValue', 'placeholder'],
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
    props: ['label'],
    emits: ['click'],
    template: '<button class="btn-stub" @click="$emit(\'click\', $event)">{{ label }}</button>'
  }
};

/**
 * Mount the panel for one session with the given backend/env state.
 * @param overrides Backend-state overrides.
 * @param env .env values the form should load.
 * @param sessionId The open session (`''` = none).
 */
async function mountPanel(
  overrides: Record<string, unknown> = {},
  env: Record<string, string> = {},
  sessionId = 'sid-1'
) {
  routeState.sid = sessionId;
  storeApi = {
    stateFor: vi.fn(() => backendState(overrides)),
    select: vi.fn(),
    hydrate: vi.fn()
  };
  vi.stubGlobal(
    'useWebSearchStore',
    vi.fn(() => storeApi)
  );
  envApi = {
    readEnvConfig: vi.fn(async () =>
      envPayload({
        SEARXNG_URL: 'http://localhost:8888',
        SEARXNG_PROXY: '',
        SEARXNG_ENGINES: '',
        SEARXNG_CATEGORIES: '',
        SEARXNG_VERIFY_TLS: 'true',
        ...env
      })
    ),
    writeEnvConfig: vi.fn(async () => true)
  };
  const wrapper = mount(WebSearchPanel, { global: { stubs } });
  await vi.waitFor(() => expect(envApi.readEnvConfig).toHaveBeenCalled());
  await wrapper.vm.$nextTick();
  return wrapper;
}

describe('WebSearchPanel.vue (integration, store + env mocked)', () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it('renders the three sections with both providers configured', async () => {
    const wrapper = await mountPanel();

    expect(wrapper.find('[data-test="web-search-backend-tavily"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="web-search-backend-searxng"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="web-search-tavily-status"]').text()).toContain('已配置');
    expect(wrapper.find('[data-test="web-search-none-configured"]').exists()).toBe(false);
    // A configured instance opens its own form (the URL is the reason to look).
    expect(wrapper.find('[data-test="web-search-searxng-form"]').exists()).toBe(true);
  });

  it('disables an unconfigured provider and points at the fix', async () => {
    const wrapper = await mountPanel({
      tavily_available: false,
      searxng: { configured: false, url: '', engines: '', categories: '', proxy: '', verify_tls: true }
    });

    // Neither provider configured: the tool is unavailable and says so.
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
    const wrapper = await mountPanel({}, { SEARXNG_URL: '' }, '');

    expect(wrapper.find('[data-test="web-search-no-session"]').exists()).toBe(true);
    // Availability still renders (the sections are global), but the rows are inert.
    expect(wrapper.find('[data-test="web-search-backend-tavily"]').attributes('disabled')).toBeDefined();
  });

  it('saves only the changed .env keys and drops the restart hint afterwards', async () => {
    const wrapper = await mountPanel({}, { SEARXNG_URL: 'http://localhost:8888' });

    // The loaded value matches the runtime one → no restart owed yet.
    expect(wrapper.find('[data-test="web-search-restart-hint"]').exists()).toBe(false);

    const input = wrapper.find('[data-test="web-search-searxng-url"]');
    await input.setValue('http://localhost:9999');
    expect(wrapper.find('[data-test="web-search-restart-hint"]').exists()).toBe(true);

    await wrapper.find('[data-test="web-search-searxng-save"]').trigger('click');
    await vi.waitFor(() => expect(envApi.writeEnvConfig).toHaveBeenCalled());

    expect(envApi.writeEnvConfig).toHaveBeenCalledWith({ SEARXNG_URL: 'http://localhost:9999' });
    await vi.waitFor(() => expect(wrapper.find('[data-test="web-search-saved"]').exists()).toBe(true));
  });

  it('reports a refused save', async () => {
    const wrapper = await mountPanel({}, { SEARXNG_PROXY: '' });
    envApi.writeEnvConfig.mockResolvedValue(false);

    const input = wrapper.find('[data-test="web-search-searxng-proxy"]');
    await input.setValue('http://127.0.0.1:7890');
    await wrapper.find('[data-test="web-search-searxng-save"]').trigger('click');

    await vi.waitFor(() => expect(wrapper.find('[data-test="web-search-save-failed"]').exists()).toBe(true));
  });
});
