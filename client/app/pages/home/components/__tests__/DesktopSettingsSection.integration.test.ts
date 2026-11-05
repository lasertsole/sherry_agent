/**
 * System Config → Desktop: the close behaviour picker writes the app flag the
 * close dialog reads (one source of truth, no backend involved).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import DesktopSettingsSection from '@/pages/home/components/DesktopSettingsSection.vue';

const flags = vi.hoisted(() => ({
  getFlag: vi.fn(),
  setFlag: vi.fn()
}));

vi.mock('~/composables/use-app-flags', () => ({
  getAppFlag: flags.getFlag,
  setAppFlag: flags.setFlag
}));
vi.mock('~/composables/bridge/transport', () => ({ isTauri: () => true }));

// `<Select>` resolves to a PrimeVue component in the app; a bare stub key does
// not replace it under Vitest, so register it as a GLOBAL component.
const SelectStub = {
  name: 'Select',
  props: ['modelValue', 'options', 'optionLabel', 'optionValue'],
  emits: ['update:modelValue', 'change'],
  template:
    '<select data-test="select" :value="modelValue" @change="$emit(\'update:modelValue\', $event.target.value); $emit(\'change\')"><option v-for="o in options" :key="o.value" :value="o.value">{{ o.label }}</option></select>'
};

const mountSection = () =>
  mount(DesktopSettingsSection, { global: { components: { Select: SelectStub } } });

beforeEach(() => {
  flags.getFlag.mockReset().mockResolvedValue(undefined);
  flags.setFlag.mockReset().mockResolvedValue(undefined);
});

describe('DesktopSettingsSection', () => {
  it('defaults to ask when no choice was stored', async () => {
    const wrapper = mountSection();
    await flushPromises();

    expect(flags.getFlag).toHaveBeenCalledWith('closeAction');
    expect((wrapper.find('[data-test="desktop-close-behavior"]').element as HTMLSelectElement).value).toBe('ask');
  });

  it('shows the remembered choice', async () => {
    flags.getFlag.mockResolvedValue('tray');
    const wrapper = mountSection();
    await flushPromises();

    expect((wrapper.find('[data-test="desktop-close-behavior"]').element as HTMLSelectElement).value).toBe('tray');
  });

  it('persists a new choice immediately', async () => {
    const wrapper = mountSection();
    await flushPromises();

    await wrapper.find('[data-test="desktop-close-behavior"]').setValue('exit');
    await flushPromises();

    expect(flags.setFlag).toHaveBeenCalledWith('closeAction', 'exit');
  });
});
