/**
 * The first-run tour: four steps, 跳过 handing over the ordinary preset dialog,
 * and 开始使用 creating the session from the chosen preset. Both exits record
 * the persistent flag, which is what keeps the tour from reopening on every
 * visit.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
import { reactive } from 'vue';

import OnboardingDialog from '../OnboardingDialog.vue';

const catalogApi = vi.hoisted(() => ({
  createSessionFromPreset: vi.fn(async () => 'new-sid'),
  presetCatalogFacts: vi.fn(() => ({ tools: [], gateableMiddlewares: [], skills: [] })),
  catalogEntries: vi.fn(() => [
    { id: 'pure', nameKey: 'personaPreset.pure', builtin: true },
    { id: 'companion', nameKey: 'personaPreset.companion', builtin: true },
    { id: 'sherry', nameKey: 'personaPreset.sherry', builtin: true }
  ]),
  entryName: vi.fn((entry: { id: string }) => `name:${entry.id}`),
  DEFAULT_PRESET_ID: 'sherry'
}));
vi.mock('@/composables/persona-catalog', () => catalogApi);

const flags = vi.hoisted(() => ({ setAppFlag: vi.fn(async () => undefined) }));
vi.mock('@/composables/use-app-flags', () => flags);

const router = vi.hoisted(() => ({ push: vi.fn(async () => {}) }));
vi.stubGlobal('useRouter', () => router);
vi.stubGlobal('useLocalePath', () => (path: string) => path);

const stubs = {
  Dialog: {
    name: 'Dialog',
    props: ['visible', 'modal', 'closable', 'dismissableMask', 'showHeader', 'style'],
    emits: ['update:visible'],
    template: '<div class="dlg" v-if="visible"><slot /></div>'
  },
  Button: {
    name: 'Button',
    props: ['label', 'disabled', 'loading'],
    emits: ['click'],
    template: '<button class="btn" :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>'
  }
};

/** Mount the tour open. */
function mountTour() {
  return mount(OnboardingDialog, { props: { visible: true }, global: { stubs } });
}

/**
 * Click by the visible (translated) label.
 * @param wrapper
 * @param label
 */
async function clickLabel(wrapper: ReturnType<typeof mount>, label: string) {
  const button = wrapper.findAll('button').find(candidate => candidate.text() === label);
  if (!button) throw new Error(`no button labelled ${label}`);
  await button.trigger('click');
  await flushPromises();
}

beforeEach(() => {
  catalogApi.createSessionFromPreset.mockClear().mockResolvedValue('new-sid');
  flags.setAppFlag.mockClear();
  router.push.mockClear();
  vi.stubGlobal('useAgentConfigStore', () =>
    reactive({ catalog: { tools: [], middlewares: [], subagent_roles: [], skills: [] }, loadCatalog: async () => {} })
  );
});

describe('OnboardingDialog', () => {
  it('walks welcome → presets → guides → ready', async () => {
    const wrapper = mountTour();

    expect(wrapper.text()).toContain('欢迎使用 Sherry');
    await clickLabel(wrapper, '下一步');
    // Every built-in is offered; the default preset starts selected.
    expect(wrapper.text()).toContain('选择一个预设开始');
    expect(wrapper.text()).toContain('name:sherry');
    expect(wrapper.text()).toContain('name:pure');
    await clickLabel(wrapper, '下一步');
    expect(wrapper.text()).toContain('核心操作');
    await clickLabel(wrapper, '下一步');
    expect(wrapper.text()).toContain('一切就绪');
    expect(wrapper.text()).toContain('开始使用');
  });

  it('跳过 records the flag, closes, and hands over the preset dialog', async () => {
    const wrapper = mountTour();

    await clickLabel(wrapper, '跳过');

    expect(flags.setAppFlag).toHaveBeenCalledWith('onboarding_completed', true);
    expect(wrapper.emitted('update:visible')?.at(-1)).toEqual([false]);
    expect(wrapper.emitted('skip')).toHaveLength(1);
    expect(catalogApi.createSessionFromPreset).not.toHaveBeenCalled();
  });

  it('开始使用 creates the session from the CHOSEN preset, then records the flag', async () => {
    const wrapper = mountTour();
    await clickLabel(wrapper, '下一步');
    await wrapper.get('[data-test="onboarding-preset-companion"]').trigger('click');
    await clickLabel(wrapper, '下一步');
    await clickLabel(wrapper, '下一步');
    await clickLabel(wrapper, '开始使用');
    await flushPromises();

    expect(catalogApi.createSessionFromPreset).toHaveBeenCalledWith(
      expect.objectContaining({ id: 'companion' }),
      expect.objectContaining({ locale: 'zh', ensureCharacter: expect.any(Function) })
    );
    expect(flags.setAppFlag).toHaveBeenCalledWith('onboarding_completed', true);
    expect(wrapper.emitted('update:visible')?.at(-1)).toEqual([false]);
  });

  it('keeps the dialog (and the flag unset) when the session cannot be created', async () => {
    catalogApi.createSessionFromPreset.mockRejectedValue(new Error('apply failed'));
    const wrapper = mountTour();
    await clickLabel(wrapper, '下一步');
    await clickLabel(wrapper, '下一步');
    await clickLabel(wrapper, '下一步');
    await clickLabel(wrapper, '开始使用');
    await flushPromises();

    expect(wrapper.find('[data-test="onboarding-error"]').text()).toContain('apply failed');
    expect(flags.setAppFlag).not.toHaveBeenCalled();
    expect(wrapper.emitted('update:visible')).toBeUndefined();
  });

  it('starts every open at the welcome step', async () => {
    const wrapper = mountTour();
    await clickLabel(wrapper, '下一步');
    expect(wrapper.text()).toContain('选择一个预设开始');

    await wrapper.setProps({ visible: false });
    await wrapper.setProps({ visible: true });

    expect(wrapper.text()).toContain('欢迎使用 Sherry');
  });
});
