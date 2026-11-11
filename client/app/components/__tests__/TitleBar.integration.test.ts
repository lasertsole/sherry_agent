/**
 * Custom title bar: geometry, window controls and the desktop/Web split.
 *
 * The desktop window has no OS decorations, so the bar's left edge must follow
 * the left column (and get out of the way when it is collapsed or the right
 * panel covers the window), and its controls must drive the Tauri window APIs.
 * The close button deliberately calls `window.close()`: that is the same
 * CloseRequested path the OS button used, so the existing CloseToTrayDialog
 * flow answers it. The Web build keeps the browser chrome and draws nothing.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { reactive } from 'vue';
import TitleBar from '@/components/TitleBar.vue';
import { TITLEBAR_HEIGHT } from '@/common/titlebar';

const windowApi = vi.hoisted(() => ({
  minimize: vi.fn(),
  toggleMaximize: vi.fn(),
  isMaximized: vi.fn(),
  close: vi.fn()
}));

vi.mock('@tauri-apps/api/window', () => ({
  getCurrentWindow: () => windowApi
}));

/** Mark the runtime as the Tauri desktop shell (isTauri reads this property). */
function enterDesktop(): void {
  (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {};
}

function leaveDesktop(): void {
  delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
}

/**
 * Replace an auto-imported store/route for one case and hand back its restore.
 * @param name Global the component reads (e.g. `useUiStore`).
 * @param value The replacement factory/object.
 * @returns A function that restores the previous global.
 */
function stubGlobal(name: string, value: unknown): () => void {
  const original = (globalThis as Record<string, unknown>)[name];
  vi.stubGlobal(name, value);
  return () => vi.stubGlobal(name, original);
}

beforeEach(() => {
  windowApi.minimize.mockReset().mockResolvedValue(undefined);
  windowApi.toggleMaximize.mockReset().mockResolvedValue(undefined);
  windowApi.isMaximized.mockReset().mockResolvedValue(false);
  windowApi.close.mockReset().mockResolvedValue(undefined);
  enterDesktop();
});

afterEach(() => {
  leaveDesktop();
});

function mountBar() {
  return mount(TitleBar);
}

describe('TitleBar.vue (integration, Tauri mocked)', () => {
  it('starts at the left column edge on the home shell with both columns in place', async () => {
    const wrapper = mountBar();
    await flushPromises();

    const bar = wrapper.get('[data-test="titlebar"]');
    // The shared default sidebar-width store is 360.
    expect(bar.attributes('style')).toContain('left: 360px');
    // The height comes from the shared constant (not a drifting Tailwind class).
    expect(bar.attributes('style')).toContain(`height: ${TITLEBAR_HEIGHT}px`);

    // One drag region plus the three window controls. The drag region is empty.
    const drag = wrapper.get('[data-test="titlebar-drag"]');
    expect(drag.attributes()).toHaveProperty('data-tauri-drag-region');
    expect(drag.text()).toBe('');
    // Windows-style maximize glyph: one outlined square in the window state.
    expect(wrapper.get('[data-test="titlebar-maximize"] svg').findAll('rect')).toHaveLength(1);
    expect(wrapper.find('[data-test="titlebar-minimize"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="titlebar-maximize"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="titlebar-close"]').exists()).toBe(true);
    // The localized labels come from the central locale table.
    expect(wrapper.get('[data-test="titlebar-minimize"]').attributes('title')).toBe('最小化');
    wrapper.unmount();
  });

  it('spans the full window when the left column is collapsed', async () => {
    const restore = stubGlobal('useUiStore', () =>
      reactive({
        sidebarCollapsed: true,
        settingsMenuOpen: false,
        sidebarBody: 'sessions',
        toggleSidebar: () => {}
      })
    );
    try {
      const wrapper = mountBar();
      expect(wrapper.get('[data-test="titlebar"]').attributes('style')).toContain('left: 0px');
      wrapper.unmount();
    } finally {
      restore();
    }
  });

  it('spans the full window while the right panel is maximized', async () => {
    const restore = stubGlobal('useRightSidebarStore', () =>
      reactive({ maximized: true, collapsed: false, width: 420, tabs: [] })
    );
    try {
      const wrapper = mountBar();
      expect(wrapper.get('[data-test="titlebar"]').attributes('style')).toContain('left: 0px');
      wrapper.unmount();
    } finally {
      restore();
    }
  });

  it('spans the full window outside the home shell (login)', async () => {
    const restore = stubGlobal('useRoute', () => ({
      path: '/login',
      fullPath: '/login',
      params: {},
      query: {}
    }));
    try {
      const wrapper = mountBar();
      expect(wrapper.get('[data-test="titlebar"]').attributes('style')).toContain('left: 0px');
      wrapper.unmount();
    } finally {
      restore();
    }
  });

  it('drives minimize / maximize / restore through the Tauri window API', async () => {
    const wrapper = mountBar();
    await flushPromises();

    await wrapper.get('[data-test="titlebar-minimize"]').trigger('click');
    await flushPromises();
    expect(windowApi.minimize).toHaveBeenCalledTimes(1);

    // Maximize → the label and icon flip to "restore".
    windowApi.isMaximized.mockResolvedValueOnce(true);
    await wrapper.get('[data-test="titlebar-maximize"]').trigger('click');
    await flushPromises();
    expect(windowApi.toggleMaximize).toHaveBeenCalledTimes(1);
    expect(wrapper.get('[data-test="titlebar-maximize"]').attributes('title')).toBe('还原');
    expect(wrapper.get('[data-test="titlebar"]').attributes('data-maximized')).toBe('true');
    // Restore glyph: the front square plus the visible part of the back one
    // (a path, because the overlap is not drawn).
    expect(wrapper.get('[data-test="titlebar-maximize"] svg').findAll('rect')).toHaveLength(1);
    expect(wrapper.get('[data-test="titlebar-maximize"] svg').findAll('path')).toHaveLength(1);
    expect(wrapper.find('[data-test="titlebar-maximize"] i').exists()).toBe(false);

    // Clicking again restores the label.
    windowApi.isMaximized.mockResolvedValueOnce(false);
    await wrapper.get('[data-test="titlebar-maximize"]').trigger('click');
    await flushPromises();
    expect(windowApi.toggleMaximize).toHaveBeenCalledTimes(2);
    expect(wrapper.get('[data-test="titlebar-maximize"]').attributes('title')).toBe('最大化');
    expect(wrapper.get('[data-test="titlebar"]').attributes('data-maximized')).toBe('false');
    wrapper.unmount();
  });

  it('closes through the existing window close request (close-to-tray flow)', async () => {
    const wrapper = mountBar();
    await flushPromises();

    await wrapper.get('[data-test="titlebar-close"]').trigger('click');
    await flushPromises();
    // `close()` is what the OS button used: the Rust shell intercepts it and
    // asks CloseToTrayDialog. No second hide/quit path is invented here.
    expect(windowApi.close).toHaveBeenCalledTimes(1);
    expect(windowApi.minimize).not.toHaveBeenCalled();
    wrapper.unmount();
  });

  it('renders nothing in the Web build and calls no window API', async () => {
    leaveDesktop();
    const wrapper = mountBar();
    await flushPromises();

    expect(wrapper.find('[data-test="titlebar"]').exists()).toBe(false);
    expect(windowApi.isMaximized).not.toHaveBeenCalled();
    wrapper.unmount();
  });
});
