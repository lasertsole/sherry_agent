/**
 * The shortcut-bindings tab of 系统设置: lists every binding with its live chord,
 * records a new one on demand, refuses a conflict, and restores defaults.
 *
 * The REAL `use-shortcuts` module runs here over fake-indexeddb (no stubs), so
 * a rebind exercised through the UI is exactly what the app persists.
 */
import 'fake-indexeddb/auto';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';

const i18n = vi.hoisted(() => ({
  t: (key: string, params?: Record<string, unknown>) => (params ? `${key} ${JSON.stringify(params)}` : key)
}));
vi.mock('vue-i18n', () => ({ useI18n: () => ({ t: i18n.t }) }));

import ShortcutSettingsSection from '../ShortcutSettingsSection.vue';
import { effectiveKeys, resetShortcutBinding } from '@/composables/use-shortcuts';

/** The mounted section (unmounted in afterEach: a stale instance still in
 *  capture mode would swallow the next test's key press). */
/** PrimeVue's Button is registered globally in the app, not in a bare mount. */
const ButtonStub = { name: 'Button', props: ['label'], template: '<button>{{ label }}</button>' };

let mounted: ReturnType<typeof mount> | null = null;
const mountSection = () => {
  mounted = mount(ShortcutSettingsSection, { attachTo: document.body, global: { stubs: { Button: ButtonStub } } });
  return mounted;
};
const comboOf = (wrapper: ReturnType<typeof mountSection>, id: string): string =>
  wrapper.find(`[data-binding-id="${id}"] [data-test="shortcut-combo"]`).text();

afterEach(async () => {
  mounted?.unmount();
  mounted = null;
  for (const id of [
    'send-message',
    'command-palette',
    'history-prev',
    'history-next',
    'cancel-input',
    'find-in-conversation',
    'new-session',
    'toggle-sidebar'
  ]) {
    await resetShortcutBinding(id);
  }
});

describe('ShortcutSettingsSection', () => {
  it('lists every binding by category with its default chord', () => {
    const wrapper = mountSection();

    expect(wrapper.text()).toContain('shortcuts.sendMessage');
    expect(wrapper.text()).toContain('shortcuts.commandPalette');
    expect(wrapper.text()).toContain('shortcuts.group.composer');
    expect(wrapper.text()).toContain('shortcuts.group.global');
    expect(comboOf(wrapper, 'send-message')).toBe('Enter');
    expect(comboOf(wrapper, 'command-palette')).toBe('Ctrl+K');
    expect(wrapper.find('[data-test="shortcut-reset-command-palette"]').exists()).toBe(false);
  });

  it('records a new chord, persists it and drops it from the capture state', async () => {
    const wrapper = mountSection();
    await wrapper.find('[data-test="shortcut-rebind-command-palette"]').trigger('click');
    expect(comboOf(wrapper, 'command-palette')).toBe('shortcuts.listening');

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'p', ctrlKey: true }));
    await flushPromises();

    expect(comboOf(wrapper, 'command-palette')).toBe('Ctrl+P');
    expect(effectiveKeys('command-palette')).toEqual({
      ctrl: true,
      shift: false,
      alt: false,
      meta: false,
      key: 'p'
    });
    // The row now offers its own reset.
    expect(wrapper.find('[data-test="shortcut-reset-command-palette"]').exists()).toBe(true);
  });

  it('refuses a chord another binding owns and keeps capturing', async () => {
    const wrapper = mountSection();
    await wrapper.find('[data-test="shortcut-rebind-send-message"]').trigger('click');

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'k', ctrlKey: true }));
    await flushPromises();

    expect(wrapper.find('[data-test="shortcut-conflict"]').text()).toContain('shortcuts.commandPalette');
    expect(effectiveKeys('send-message')).toEqual({ ctrl: false, shift: false, alt: false, meta: false, key: 'enter' });
    expect(comboOf(wrapper, 'send-message')).toBe('shortcuts.listening');
  });

  it('waits while only a modifier is held down', async () => {
    const wrapper = mountSection();
    await wrapper.find('[data-test="shortcut-rebind-command-palette"]').trigger('click');

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Shift', shiftKey: true }));
    await flushPromises();

    expect(comboOf(wrapper, 'command-palette')).toBe('shortcuts.listening');
  });

  it('cancels the capture with Esc and with the button', async () => {
    const wrapper = mountSection();
    await wrapper.find('[data-test="shortcut-rebind-command-palette"]').trigger('click');
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    await flushPromises();
    expect(comboOf(wrapper, 'command-palette')).toBe('Ctrl+K');

    await wrapper.find('[data-test="shortcut-rebind-command-palette"]').trigger('click');
    await wrapper.find('[data-test="shortcut-rebind-command-palette"]').trigger('click');
    expect(comboOf(wrapper, 'command-palette')).toBe('Ctrl+K');
  });

  it('restores one default and all defaults', async () => {
    const wrapper = mountSection();
    await wrapper.find('[data-test="shortcut-rebind-command-palette"]').trigger('click');
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'p', ctrlKey: true }));
    await flushPromises();
    expect(comboOf(wrapper, 'command-palette')).toBe('Ctrl+P');

    await wrapper.find('[data-test="shortcut-reset-command-palette"]').trigger('click');
    await flushPromises();
    expect(comboOf(wrapper, 'command-palette')).toBe('Ctrl+K');

    await wrapper.find('[data-test="shortcut-rebind-command-palette"]').trigger('click');
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'p', ctrlKey: true }));
    await flushPromises();
    await wrapper.find('[data-test="shortcut-reset-all"]').trigger('click');
    await flushPromises();
    expect(comboOf(wrapper, 'command-palette')).toBe('Ctrl+K');
  });
});
