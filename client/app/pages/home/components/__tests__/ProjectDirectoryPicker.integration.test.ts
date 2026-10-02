/**
 * The in-app folder picker (browser build): navigate, then choose the folder
 * you are standing in.
 *
 * The contract: the dialog opens at the session's bound folder (or the server
 * user's home when unbound), lists one level of DIRECTORIES through
 * `/system/dirs`, walks up with a defined stop at the filesystem root, and binds
 * the current folder through the store — a rejected bind keeps the dialog open
 * with the reason, a failed listing never binds anything.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { setActivePinia } from 'pinia';
import { createTestingPinia } from '@pinia/testing';
import ProjectDirectoryPicker from '@/pages/home/components/ProjectDirectoryPicker.vue';
import { useProjectDirectoryStore } from '@/stores/project-directory';

const bridge = vi.hoisted(() => ({
  fetchProjectDirectory: vi.fn(),
  setProjectDirectory: vi.fn()
}));

vi.mock('~/composables/bridge/session', () => bridge);

// The component consumes `fetchSystemDirs` as an auto-import, which the unimport
// transform rewrites into a static import from the `~/composables/bridge` barrel
// (see ProjectFileTree.integration.test.ts) — so the module, not a global, is mocked.
const bridgeModule = vi.hoisted(() => ({ fetchSystemDirs: vi.fn() }));
vi.mock('~/composables/bridge', () => bridgeModule);

/** A tiny fake filesystem, keyed by absolute path ('' = the server user's home). */
const LEVELS: Record<string, { path: string; parent: string | null; entries: { name: string; path: string }[] }> = {
  '': {
    path: '/home/me',
    parent: '/home',
    entries: [
      { name: 'proj-x', path: '/home/me/proj-x' },
      { name: 'docs', path: '/home/me/docs' }
    ]
  },
  '/home/me': {
    path: '/home/me',
    parent: '/home',
    entries: [
      { name: 'proj-x', path: '/home/me/proj-x' },
      { name: 'docs', path: '/home/me/docs' }
    ]
  },
  '/home': {
    path: '/home',
    parent: '/',
    entries: [{ name: 'me', path: '/home/me' }]
  },
  '/home/me/proj-x': { path: '/home/me/proj-x', parent: '/home/me', entries: [] },
  '/home/me/docs': { path: '/home/me/docs', parent: '/home/me', entries: [] },
  '/': { path: '/', parent: null, entries: [{ name: 'home', path: '/home' }] }
};

const stubs = {
  Dialog: {
    name: 'Dialog',
    props: ['visible', 'header'],
    template: '<div v-if="visible" class="dlg"><slot /></div>'
  },
  Button: {
    props: ['label', 'disabled'],
    emits: ['click'],
    template: '<button class="btn" :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>'
  },
  ProgressSpinner: { template: '<span class="spin" />' }
};

const stateOf = (dir: string | null) => ({
  directory: dir,
  effective: dir ?? '/repo',
  source: dir ? 'session' : 'default',
  pendingDirectory: null
});

async function mountPicker(visible = true, initialPath = '/home/me') {
  const wrapper = mount(ProjectDirectoryPicker, {
    props: { sessionId: 'sid-1', initialPath, visible },
    global: { stubs }
  });
  await flushPromises();
  return wrapper;
}

describe('ProjectDirectoryPicker', () => {
  beforeEach(() => {
    setActivePinia(createTestingPinia({ stubActions: false }));
    vi.stubGlobal('useProjectDirectoryStore', () => useProjectDirectoryStore());
    bridge.fetchProjectDirectory.mockReset();
    bridge.setProjectDirectory.mockReset();
    bridge.fetchProjectDirectory.mockResolvedValue(stateOf(null));
    bridgeModule.fetchSystemDirs.mockReset();
    bridgeModule.fetchSystemDirs.mockImplementation(async (path = '') => {
      const level = LEVELS[path];
      if (!level) throw new Error('system folder request failed');
      return { ...level, truncated: false, total: level.entries.length };
    });
  });

  it('starts at the session folder and lists its subfolders', async () => {
    const wrapper = await mountPicker(true, '/home/me');

    expect(bridgeModule.fetchSystemDirs).toHaveBeenCalledWith('/home/me');
    expect(wrapper.get('[data-test="dir-picker-path"]').text()).toBe('/home/me');
    const names = wrapper.findAll('[data-test="dir-picker-entry"]').map(e => e.attributes('data-name'));
    expect(names).toEqual(['proj-x', 'docs']);
  });

  it('starts at the server user home when nothing is bound', async () => {
    const wrapper = await mountPicker(true, '');

    expect(bridgeModule.fetchSystemDirs).toHaveBeenCalledWith('');
    expect(wrapper.get('[data-test="dir-picker-path"]').text()).toBe('/home/me');
  });

  it('enters a folder on click and shows its (empty) level', async () => {
    const wrapper = await mountPicker();

    await wrapper.get('[data-name="proj-x"]').trigger('click');
    await flushPromises();

    expect(bridgeModule.fetchSystemDirs).toHaveBeenLastCalledWith('/home/me/proj-x');
    expect(wrapper.get('[data-test="dir-picker-path"]').text()).toBe('/home/me/proj-x');
    expect(wrapper.find('[data-test="dir-picker-empty"]').exists()).toBe(true);
  });

  it('walks up one level, and stops at the filesystem root', async () => {
    const wrapper = await mountPicker();

    await wrapper.get('[data-test="dir-picker-up"]').trigger('click');
    await flushPromises();
    expect(wrapper.get('[data-test="dir-picker-path"]').text()).toBe('/home');

    await wrapper.get('[data-test="dir-picker-up"]').trigger('click');
    await flushPromises();
    expect(wrapper.get('[data-test="dir-picker-path"]').text()).toBe('/');

    // Root has no parent: the button is disabled rather than walking nowhere.
    expect(wrapper.get('[data-test="dir-picker-up"]').attributes('disabled')).toBeDefined();
  });

  it('binds the folder it is standing in and closes', async () => {
    bridge.setProjectDirectory.mockResolvedValue({
      ok: true,
      pending: false,
      state: stateOf('/home/me/docs')
    });
    const wrapper = await mountPicker();

    await wrapper.get('[data-name="docs"]').trigger('click');
    await flushPromises();
    await wrapper.get('[data-test="dir-picker-select"]').trigger('click');
    await flushPromises();

    expect(bridge.setProjectDirectory).toHaveBeenCalledWith('sid-1', '/home/me/docs');
    expect(wrapper.emitted('update:visible')?.at(-1)).toEqual([false]);
  });

  it('keeps the dialog open and shows the reason when the bind is rejected', async () => {
    bridge.setProjectDirectory.mockRejectedValue(new Error('project directory cannot be resolved'));
    const wrapper = await mountPicker();

    await wrapper.get('[data-test="dir-picker-select"]').trigger('click');
    await flushPromises();

    expect(bridge.setProjectDirectory).toHaveBeenCalledWith('sid-1', '/home/me');
    expect(wrapper.emitted('update:visible')).toBeUndefined();
    expect(wrapper.get('[data-test="dir-picker-bind-error"]').text()).toContain('cannot be resolved');
  });

  it('reports a listing failure without binding anything', async () => {
    bridgeModule.fetchSystemDirs.mockRejectedValue(new Error('system folder request failed'));
    const wrapper = await mountPicker();

    expect(wrapper.get('[data-test="dir-picker-error"]').text()).toContain('system folder request failed');
    await wrapper.get('[data-test="dir-picker-select"]').trigger('click');
    await flushPromises();
    expect(bridge.setProjectDirectory).not.toHaveBeenCalled();
  });
});
