/**
 * Composer toolbar thinning (integration, backend mocked).
 *
 * With both sidebars open the chat column narrows to ~280px while the toolbar
 * row above the input box needs ~472px, so its right side spilled out of the
 * column. The row now thins below 500px of container width to the three anchors
 * the user asked for — the media (+) entry, the access-mode list and the
 * project-directory chip — hiding the task badge, the context ring, the model
 * picker and the thinking control.
 *
 * Container queries cannot be evaluated in happy-dom, so this suite pins the
 * CONTRACT at the class level (same approach as the shell's top-toolbar thinning
 * test): exactly the four optional controls carry `@max-[500px]:hidden!` and the
 * three anchors carry no variant. The `!` is load-bearing — PrimeVue's own
 * `display` rules are unlayered and outrank a layered utility. Live-verified in
 * the browser: at a 280px column only the three anchors render (row overflow
 * false), at 1600px every control is back.
 */
import { describe, it, expect, vi } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import sidPage from '@/pages/home/index/[sid].vue';
import MediaMenu from '@/pages/home/components/MediaMenu.vue';

const NARROW_SEL = '[class*="@max-[500px]:hidden!"]';

vi.mock('vue-router', () => {
  const useRoute = () => ({ params: { sid: 'default' }, fullPath: '/home/default' });
  const useRouter = () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn(), go: vi.fn() });
  return {
    useRoute,
    useRouter,
    RouterLink: { name: 'RouterLink', props: ['to'], template: '<a><slot /></a>' },
    RouterView: { name: 'RouterView', template: '<div><slot /></div>' }
  };
});

vi.mock('@/composables/db', async importOriginal => {
  const actual = await importOriginal<typeof import('@/composables/db')>();
  return {
    ...actual,
    readCachedMessages: async () => [],
    cachedMaxTurnNum: async () => 0,
    cacheMessages: async () => {},
    clearCachedSession: async () => {},
    readCachedCharacter: async () => undefined,
    cacheCharacter: async () => {},
    clearCachedCharacter: async () => {},
    readCachedSessionMetaList: async () => [],
    cacheSessionMeta: async () => {},
    clearCachedSessionMeta: async () => {},
    saveSessionTitleOverride: async () => {},
    readSessionTitleOverrides: async () => new Map<string, string>(),
    clearSessionTitleOverride: async () => {},
    saveDraftTurn: async () => {},
    readDraftTurns: async () => [],
    clearDraftTurn: async () => {},
    clearDraftSession: async () => {}
  };
});

vi.mock('@/pages/home/components/SubagentTasksView.vue', () => ({
  default: { name: 'SubagentTasksView', template: '<div class="stv-stub"></div>' }
}));

const fetchApiMock = vi.hoisted(() => vi.fn());
vi.mock('@/composables/requestApi', () => ({
  fetchApi: fetchApiMock,
  // The toolbar's chips route through the gateway token helpers; stub them so
  // the mocked module keeps the shape its consumers expect.
  ensureGatewayToken: async () => null,
  withGatewayToken: (url: string) => url
}));

const primevueStub = {
  Checkbox: {
    name: 'Checkbox',
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<button class="cb" @click="$emit(\'update:modelValue\', !modelValue)">C</button>'
  },
  Button: { props: ['label'], template: '<button class="btn"><slot /><span>{{ label }}</span></button>' },
  Menu: { template: '<div class="mnu"></div>', methods: { toggle() {} } },
  ToggleSwitch: { template: '<span class="ts"></span>' },
  ChatInputBox: { template: '<div class="cib"></div>' }
};

describe('composer toolbar thinning (integration, backend mocked)', () => {
  it('marks exactly the four optional controls, leaving the three anchors alone', async () => {
    fetchApiMock.mockResolvedValue({ code: 200, data: [] });
    const wrapper = mount(sidPage, { global: { stubs: primevueStub } });
    await flushPromises();

    // The row itself is the query container the variants measure against.
    // (Selector-escape-free lookup: the row is the media entry's parent.)
    const row = wrapper.findComponent(MediaMenu).element.parentElement as HTMLElement;
    expect(row.className).toContain('@container');

    // Exactly the four optional controls carry the variant…
    const thinned = wrapper.findAll(NARROW_SEL);
    expect(thinned).toHaveLength(4);
    const thinnedLabels = thinned.map(w => w.find('button').attributes('aria-label') ?? '');
    expect(thinnedLabels.some(label => label.includes('后台任务'))).toBe(true); // TasksButton
    expect(thinnedLabels.some(label => label.includes('上下文'))).toBe(true); // ContextUsageButton
    expect(thinnedLabels.some(label => label.includes('主模型'))).toBe(true); // SessionModelPicker
    expect(thinnedLabels.some(label => label.includes('思考'))).toBe(true); // ThinkingToggle

    // …and the three anchors the user keeps do not.
    expect(wrapper.find('button[aria-label="选择要上传的多媒体类型"]').element.closest(NARROW_SEL)).toBeNull();
    expect(wrapper.find('button[aria-label^="访问模式"]').element.closest(NARROW_SEL)).toBeNull();
    expect(wrapper.find('[data-test="project-dir-trigger"]').element.closest(NARROW_SEL)).toBeNull();
  });
});
