/**
 * The replay-gate allowlist editor: it reads/writes ONE list setting and never
 * shows (or removes) the built-in defaults — the user only extends them.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { mount } from '@vue/test-utils';

const bridge = vi.hoisted(() => ({
  readSherryList: vi.fn(),
  writeSherryList: vi.fn()
}));
vi.mock('~/composables/sherryConfig', () => bridge);

// The component calls `useI18n` from vue-i18n directly, and the test env loads
// the real locales — so stub the module and assert on KEYS (locale-independent).
const i18n = vi.hoisted(() => ({
  t: (key: string, params?: Record<string, unknown>) => (params ? `${key}:${JSON.stringify(params)}` : key)
}));
vi.mock('vue-i18n', () => ({ useI18n: () => ({ t: i18n.t }) }));

import ReplayAllowlistEditor from '../ReplayAllowlistEditor.vue';

const stubs = {
  Button: {
    props: ['label', 'disabled', 'loading'],
    template: '<button :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>'
  },
  InputText: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<input :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />'
  }
};

function mountEditor() {
  return mount(ReplayAllowlistEditor, { global: { stubs } });
}

describe('ReplayAllowlistEditor', () => {
  beforeEach(() => {
    bridge.readSherryList.mockReset();
    bridge.writeSherryList.mockReset();
    bridge.readSherryList.mockResolvedValue(['gradle']);
    bridge.writeSherryList.mockImplementation(async (_key: string, values: string[]) => values);
  });

  it('loads the user entries (never the built-in defaults)', async () => {
    const wrapper = mountEditor();
    await vi.waitFor(() => expect(bridge.readSherryList).toHaveBeenCalled());

    expect(bridge.readSherryList).toHaveBeenCalledWith('regression_gate_allowed_executables');
    await vi.waitFor(() => expect(wrapper.text()).toContain('gradle'));
  });

  it('adds a normalized entry and persists the whole list', async () => {
    const wrapper = mountEditor();
    await vi.waitFor(() => expect(wrapper.text()).toContain('gradle'));

    const input = wrapper.find('[data-test="replay-allowlist-input"]');
    await input.setValue('  Maven ');
    await wrapper.find('[data-test="replay-allowlist-add"]').trigger('click');

    await vi.waitFor(() =>
      expect(bridge.writeSherryList).toHaveBeenCalledWith('regression_gate_allowed_executables', ['gradle', 'maven'])
    );
  });

  it('removes one entry and persists the remainder', async () => {
    const wrapper = mountEditor();
    await vi.waitFor(() => expect(wrapper.text()).toContain('gradle'));

    await wrapper.find('button[aria-label="config.sherry.allowlist.remove"]').trigger('click');

    await vi.waitFor(() =>
      expect(bridge.writeSherryList).toHaveBeenCalledWith('regression_gate_allowed_executables', [])
    );
  });

  it('shows the empty state when nothing is configured', async () => {
    bridge.readSherryList.mockResolvedValue([]);
    const wrapper = mountEditor();

    await vi.waitFor(() => expect(wrapper.find('[data-test="replay-allowlist-empty"]').exists()).toBe(true));
  });

  it('refuses an entry with shell metacharacters before it reaches the backend', async () => {
    const wrapper = mountEditor();
    await vi.waitFor(() => expect(wrapper.text()).toContain('gradle'));

    await wrapper.find('[data-test="replay-allowlist-input"]').setValue('gradle; rm -rf /');
    const add = wrapper.find('[data-test="replay-allowlist-add"]');
    expect(add.attributes('disabled')).toBeDefined();
    await add.trigger('click');

    expect(bridge.writeSherryList).not.toHaveBeenCalled();
  });

  it('surfaces a read failure instead of pretending the list is empty', async () => {
    bridge.readSherryList.mockRejectedValue(new Error('offline'));
    const wrapper = mountEditor();

    await vi.waitFor(() => expect(wrapper.text()).toContain('config.sherry.allowlist.loadFailed'));
  });
});
