/**
 * The project-directory chip: three states, a hydrating popover, manual entry
 * and in-place errors.
 *
 * The observable contract: the chip names the bound directory (basename) or
 * warns when nothing is bound, the clock marks a parked choice, opening the
 * popover re-reads the backend, and a submitted path goes straight to the store
 * (which owns validation-by-server and rollback) — the component never invents
 * a path.
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
  InputText: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<input class="it" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />'
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

  it('submits a manually typed path through the store', async () => {
    const wrapper = await mountChip();
    bridge.setProjectDirectory.mockResolvedValue({
      ok: true,
      pending: false,
      state: stateOf('/home/me/typed')
    });

    await wrapper.get('[data-test="project-dir-input"]').setValue('/home/me/typed');
    await wrapper.get('[data-test="project-dir-confirm"]').trigger('click');
    await flushPromises();

    expect(bridge.setProjectDirectory).toHaveBeenCalledWith('sid-1', '/home/me/typed');
  });

  it('rejects a relative path locally, before any request', async () => {
    const wrapper = await mountChip();

    await wrapper.get('[data-test="project-dir-input"]').setValue('relative/path');
    await wrapper.get('[data-test="project-dir-confirm"]').trigger('click');
    await flushPromises();

    expect(bridge.setProjectDirectory).not.toHaveBeenCalled();
    expect(wrapper.get('[data-test="project-dir-error"]').text()).toContain('绝对路径');
  });

  it('renders a server rejection in place', async () => {
    const wrapper = await mountChip();
    bridge.setProjectDirectory.mockRejectedValue(new Error('project directory cannot be resolved'));

    await wrapper.get('[data-test="project-dir-input"]').setValue('/gone/away');
    await wrapper.get('[data-test="project-dir-confirm"]').trigger('click');
    await flushPromises();

    expect(wrapper.get('[data-test="project-dir-error"]').text()).toContain('cannot be resolved');
  });

  it('offers the native picker only inside the Tauri runtime', async () => {
    const wrapper = await mountChip();
    expect(wrapper.find('[data-test="project-dir-browse"]').exists()).toBe(false);

    (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {};
    try {
      const tauriWrapper = await mountChip();
      expect(tauriWrapper.find('[data-test="project-dir-browse"]').exists()).toBe(true);
    } finally {
      delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
    }
  });
});
