/**
 * The project-directory chip: three states, a hydrating popover, and one
 * "choose a folder" action per runtime.
 *
 * The observable contract: the chip names the bound directory (basename) or
 * warns when nothing is bound, the clock marks a parked choice, opening the
 * popover re-reads the backend, and the action opens the OS folder dialog in
 * the desktop build — the in-app picker dialog in the browser build (a browser
 * cannot hand out an absolute path, so there is no typing channel anymore).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { setActivePinia } from 'pinia';
import { createTestingPinia } from '@pinia/testing';
import ProjectDirectoryChip from '@/pages/home/components/ProjectDirectoryChip.vue';
import { useProjectDirectoryStore } from '@/stores/project-directory';

const bridge = vi.hoisted(() => ({
  fetchProjectDirectory: vi.fn(),
  setProjectDirectory: vi.fn()
}));

vi.mock('~/composables/bridge/session', () => bridge);

const pickNative = vi.hoisted(() => vi.fn());

const stubs = {
  ToolbarPopover: {
    name: 'ToolbarPopover',
    template: '<div class="tp"><slot name="trigger" :toggle="() => {}" :open="false" /><slot /></div>'
  },
  Button: {
    props: ['label', 'disabled'],
    emits: ['click'],
    template: '<button class="btn" :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>'
  },
  ProjectDirectoryPicker: {
    name: 'ProjectDirectoryPicker',
    props: ['sessionId', 'initialPath', 'visible'],
    template: '<div v-if="visible" data-test="picker-stub" :data-session="sessionId" :data-initial="initialPath" />'
  }
};

const stateOf = (dir: string | null, pending: string | null = null) => ({
  directory: dir,
  effective: dir ?? '/repo',
  source: dir ? 'session' : 'default',
  pendingDirectory: pending
});

async function mountChip(sessionId = 'sid-1') {
  const wrapper = mount(ProjectDirectoryChip, {
    props: { sessionId },
    global: { stubs }
  });
  await flushPromises();
  return wrapper;
}

describe('ProjectDirectoryChip', () => {
  beforeEach(() => {
    setActivePinia(createTestingPinia({ stubActions: false }));
    // Drive the REAL store (the global stub in setup.ts serves page-mount suites).
    vi.stubGlobal('useProjectDirectoryStore', () => useProjectDirectoryStore());
    // `pickProjectDirectory` is auto-imported from utils/; the browser case never
    // reaches it, the desktop case asserts on it.
    pickNative.mockReset();
    vi.stubGlobal('pickProjectDirectory', pickNative);
    bridge.fetchProjectDirectory.mockReset();
    bridge.setProjectDirectory.mockReset();
    bridge.fetchProjectDirectory.mockResolvedValue(stateOf(null));
  });

  it('warns when no directory is bound', async () => {
    const wrapper = await mountChip();

    const trigger = wrapper.get('[data-test="project-dir-trigger"]');
    expect(trigger.text()).toContain('未绑定项目目录');
    expect(trigger.classes().join(' ')).toContain('amber');
  });

  it('names the bound directory by basename with the full path as tooltip', async () => {
    bridge.fetchProjectDirectory.mockResolvedValue(stateOf('/home/me/proj-x'));

    const wrapper = await mountChip();
    await flushPromises();

    const trigger = wrapper.get('[data-test="project-dir-trigger"]');
    expect(trigger.text()).toContain('proj-x');
    expect(trigger.attributes('title')).toBe('/home/me/proj-x');
  });

  it('marks a parked choice with the clock icon', async () => {
    bridge.fetchProjectDirectory.mockResolvedValue(stateOf('/home/me/a', '/home/me/b'));

    const wrapper = await mountChip();
    await flushPromises();

    expect(wrapper.find('[data-test="project-dir-pending"]').exists()).toBe(true);
    expect(wrapper.get('[data-test="project-dir-panel"]').text()).toContain('/home/me/b');
  });

  it('offers exactly one action and no path input', async () => {
    const wrapper = await mountChip();

    expect(wrapper.find('[data-test="project-dir-browse"]').exists()).toBe(true);
    expect(wrapper.find('input').exists()).toBe(false);
  });

  it('opens the in-app folder picker in the browser build, starting at the bound folder', async () => {
    bridge.fetchProjectDirectory.mockResolvedValue(stateOf('/home/me/proj-x'));
    const wrapper = await mountChip();
    await flushPromises();

    expect(wrapper.find('[data-test="picker-stub"]').exists()).toBe(false);
    await wrapper.get('[data-test="project-dir-browse"]').trigger('click');
    await flushPromises();

    const picker = wrapper.get('[data-test="picker-stub"]');
    expect(picker.attributes('data-session')).toBe('sid-1');
    expect(picker.attributes('data-initial')).toBe('/home/me/proj-x');
    expect(pickNative).not.toHaveBeenCalled();
  });

  it('opens the OS folder dialog in the desktop build and binds the picked path', async () => {
    (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {};
    pickNative.mockResolvedValue('/home/me/picked');
    bridge.setProjectDirectory.mockResolvedValue({
      ok: true,
      pending: false,
      state: stateOf('/home/me/picked')
    });
    try {
      const wrapper = await mountChip();

      await wrapper.get('[data-test="project-dir-browse"]').trigger('click');
      await flushPromises();

      expect(pickNative).toHaveBeenCalledTimes(1);
      expect(bridge.setProjectDirectory).toHaveBeenCalledWith('sid-1', '/home/me/picked');
      // The OS dialog owns the flow on desktop: no in-app picker on top of it.
      expect(wrapper.find('[data-test="picker-stub"]').exists()).toBe(false);
    } finally {
      delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
    }
  });

  it('binds nothing when the OS dialog is cancelled', async () => {
    (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {};
    pickNative.mockResolvedValue(null);
    try {
      const wrapper = await mountChip();

      await wrapper.get('[data-test="project-dir-browse"]').trigger('click');
      await flushPromises();

      expect(bridge.setProjectDirectory).not.toHaveBeenCalled();
    } finally {
      delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
    }
  });

  it('renders a server rejection in place', async () => {
    (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {};
    pickNative.mockResolvedValue('/gone/away');
    bridge.setProjectDirectory.mockRejectedValue(new Error('project directory cannot be resolved'));
    try {
      const wrapper = await mountChip();

      await wrapper.get('[data-test="project-dir-browse"]').trigger('click');
      await flushPromises();

      expect(wrapper.get('[data-test="project-dir-error"]').text()).toContain('cannot be resolved');
    } finally {
      delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
    }
  });
});
