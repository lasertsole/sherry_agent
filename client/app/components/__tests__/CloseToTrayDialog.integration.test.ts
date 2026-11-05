/**
 * Close-to-tray prompt (desktop): acknowledge FIRST, then remember or ask.
 *
 * The Rust shell prevents the default close, emits `close-requested` and hides
 * the window after 1.5 s unless the frontend acknowledges. These tests pin the
 * three outcomes — remembered tray, remembered exit, first-time prompt — and
 * the ordering that keeps the fallback from concealing the dialog.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { nextTick } from 'vue';
import CloseToTrayDialog from '@/components/CloseToTrayDialog.vue';

const flags = vi.hoisted(() => ({
  getFlag: vi.fn(),
  setFlag: vi.fn()
}));
const tauri = vi.hoisted(() => ({
  invoke: vi.fn(),
  hide: vi.fn(),
  listen: vi.fn(),
  unlisten: vi.fn()
}));

vi.mock('~/composables/use-app-flags', () => ({
  getAppFlag: flags.getFlag,
  setAppFlag: flags.setFlag
}));
vi.mock('~/composables/bridge/transport', () => ({ isTauri: () => true }));
vi.mock('@tauri-apps/api/core', () => ({ invoke: tauri.invoke }));
vi.mock('@tauri-apps/api/window', () => ({
  getCurrentWindow: () => ({ hide: tauri.hide })
}));
vi.mock('@tauri-apps/api/event', () => ({ listen: tauri.listen }));

const stubs = {
  Dialog: {
    props: ['visible', 'header'],
    template: '<div v-if="visible" data-test="dialog"><slot /><slot name="footer" /></div>'
  },
  Checkbox: {
    props: ['modelValue', 'binary', 'inputId'],
    emits: ['update:modelValue'],
    template:
      '<input type="checkbox" data-test="remember" :checked="modelValue" @change="$emit(\'update:modelValue\', $event.target.checked)" />'
  },
  Button: {
    props: ['label'],
    emits: ['click'],
    template: '<button data-test="button" @click="$emit(\'click\')">{{ label }}</button>'
  }
};

let closeHandler: (() => void) | null = null;

beforeEach(() => {
  flags.getFlag.mockReset();
  flags.setFlag.mockReset().mockResolvedValue(undefined);
  tauri.invoke.mockReset().mockResolvedValue(undefined);
  tauri.hide.mockReset().mockResolvedValue(undefined);
  tauri.unlisten.mockReset();
  tauri.listen.mockReset().mockImplementation(async (_name: string, handler: () => void) => {
    closeHandler = handler;
    return tauri.unlisten;
  });
  closeHandler = null;
});

async function mountDialog() {
  const wrapper = mount(CloseToTrayDialog, { global: { stubs } });
  await flushPromises();
  return wrapper;
}

function fireClose() {
  closeHandler?.();
}

describe('CloseToTrayDialog', () => {
  it('acknowledges the close before deciding', async () => {
    flags.getFlag.mockResolvedValue(undefined);
    await mountDialog();

    fireClose();
    await flushPromises();

    expect(tauri.invoke).toHaveBeenCalledWith('close_ack');
  });

  it('hides without asking when the preference is tray', async () => {
    flags.getFlag.mockResolvedValue('tray');
    const wrapper = await mountDialog();

    fireClose();
    await flushPromises();

    expect(tauri.hide).toHaveBeenCalledTimes(1);
    expect(wrapper.find('[data-test="close-tray-dialog"]').exists()).toBe(false);
  });

  it('quits without asking when the preference is exit', async () => {
    flags.getFlag.mockResolvedValue('exit');
    await mountDialog();

    fireClose();
    await flushPromises();

    expect(tauri.invoke).toHaveBeenCalledWith('quit_app');
    expect(tauri.hide).not.toHaveBeenCalled();
  });

  it('prompts, remembers the choice and hides', async () => {
    flags.getFlag.mockResolvedValue(undefined);
    const wrapper = await mountDialog();

    fireClose();
    await flushPromises();
    await vi.waitFor(() => {
      if (!wrapper.find('[data-test="close-tray-dialog"]').exists()) throw new Error('dialog not open yet');
    });

    await wrapper.find('[data-test="remember"]').setValue(true);
    await wrapper.find('[data-test="close-tray-minimize"]').trigger('click');
    await flushPromises();

    expect(flags.setFlag).toHaveBeenCalledWith('closeAction', 'tray');
    expect(tauri.hide).toHaveBeenCalledTimes(1);
    expect(wrapper.find('[data-test="close-tray-dialog"]').exists()).toBe(false);
  });

  it('does not persist when "don\'t ask again" is unticked', async () => {
    flags.getFlag.mockResolvedValue(undefined);
    const wrapper = await mountDialog();

    fireClose();
    await flushPromises();
    await wrapper.find('[data-test="close-tray-quit"]').trigger('click');
    await flushPromises();

    expect(flags.setFlag).not.toHaveBeenCalled();
    expect(tauri.invoke).toHaveBeenCalledWith('quit_app');
  });

  it('unlistens on unmount', async () => {
    const wrapper = await mountDialog();
    wrapper.unmount();
    expect(tauri.unlisten).toHaveBeenCalled();
  });
});
