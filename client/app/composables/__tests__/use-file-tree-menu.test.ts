/**
 * The file tree's right-click menu: which entries exist per row / runtime, and
 * which handler each one fires.
 *
 * The menu is capability-gated — desktop-only entries (reveal, open with the
 * system app) disappear in the web build, the browser entry exists only for
 * local HTML pages — and every entry's `command` is checked against the handler
 * it must invoke.
 */
import { describe, it, expect, vi } from 'vitest';
import {
  absolutePathOf,
  buildFileTreeMenu,
  canOpenInBuiltInBrowser,
  mentionPayloadOf,
  type FileTreeMenuContext
} from '../use-file-tree-menu';

function makeContext(overrides: Partial<FileTreeMenuContext> = {}): FileTreeMenuContext {
  return {
    node: { type: 'file', path: 'src/foo.py', name: 'foo.py' },
    sessionId: 'sid-1',
    rootPath: '/proj',
    desktop: false,
    labels: {
      open: 'Open',
      openWith: 'Open with',
      openInViewer: 'Viewer',
      openWithSystem: 'System',
      openInBrowser: 'Browser',
      reveal: 'Reveal',
      copyAbsolutePath: 'Copy abs',
      copyRelativePath: 'Copy rel',
      addToChat: 'Add to chat'
    },
    open: vi.fn(),
    openSystem: vi.fn(),
    openBrowser: vi.fn(),
    reveal: vi.fn(),
    copy: vi.fn(),
    addToChat: vi.fn(),
    ...overrides
  };
}

/**
 * The menu's item labels, separators dropped (they only split the groups).
 * @param context
 */
function labelsOf(context: FileTreeMenuContext): string[] {
  return buildFileTreeMenu(context)
    .filter(item => !item.separator)
    .map(item => item.label ?? '');
}

describe('canOpenInBuiltInBrowser', () => {
  it('accepts local HTML pages, case-insensitively', () => {
    expect(canOpenInBuiltInBrowser({ type: 'file', path: 'a/index.html', name: 'index.html' })).toBe(true);
    expect(canOpenInBuiltInBrowser({ type: 'file', path: 'a/INDEX.HTM', name: 'INDEX.HTM' })).toBe(true);
  });

  it('refuses non-HTML files and directories', () => {
    expect(canOpenInBuiltInBrowser({ type: 'file', path: 'a/app.py', name: 'app.py' })).toBe(false);
    expect(canOpenInBuiltInBrowser({ type: 'dir', path: 'a', name: 'a' })).toBe(false);
  });
});

describe('absolutePathOf', () => {
  it('joins the root and a relative path', () => {
    expect(absolutePathOf('/proj', 'src/foo.py')).toBe('/proj/src/foo.py');
  });

  it('does not double a trailing separator', () => {
    expect(absolutePathOf('/proj/', 'src/foo.py')).toBe('/proj/src/foo.py');
  });

  it('keeps a Windows root in its own separator style', () => {
    expect(absolutePathOf('C:\\proj', 'src/foo.py')).toBe('C:\\proj\\src\\foo.py');
  });

  it('returns empty for an unknown root', () => {
    expect(absolutePathOf('', 'src/foo.py')).toBe('');
  });
});

describe('mentionPayloadOf', () => {
  it('carries the row type, path and name', () => {
    expect(mentionPayloadOf({ type: 'dir', path: 'src', name: 'src' })).toEqual({
      type: 'dir',
      path: 'src',
      name: 'src'
    });
  });
});

describe('buildFileTreeMenu — web build', () => {
  it('lists open, open-with, copy and add-to-chat; no reveal, no system app', () => {
    const labels = labelsOf(makeContext());
    expect(labels).toEqual(['Open', 'Open with', 'Copy abs', 'Copy rel', 'Add to chat']);
  });

  it('shows the browser entry for an HTML file, standalone and in the submenu', () => {
    const context = makeContext({
      node: { type: 'file', path: 'index.html', name: 'index.html' }
    });
    const items = buildFileTreeMenu(context);
    const labels = labelsOf(context);
    expect(labels).toContain('Browser');
    const openWith = items.find(item => item.label === 'Open with');
    expect(openWith?.items?.map(item => item.label)).toContain('Browser');
    // Two entries fire the same handler (ZCode has both too).
    const browserEntry = items.find(item => item.label === 'Browser');
    browserEntry?.command?.();
    expect(context.openBrowser).toHaveBeenCalledTimes(1);
  });

  it('has no open-with submenu for a directory', () => {
    const context = makeContext({ node: { type: 'dir', path: 'src', name: 'src' } });
    expect(labelsOf(context)).not.toContain('Open with');
  });

  it('fires open for the first entry', () => {
    const context = makeContext();
    buildFileTreeMenu(context)[0]?.command?.();
    expect(context.open).toHaveBeenCalledTimes(1);
  });

  it('copies the absolute path and the relative path from their entries', () => {
    const context = makeContext();
    const items = buildFileTreeMenu(context);
    items.find(item => item.label === 'Copy abs')?.command?.();
    items.find(item => item.label === 'Copy rel')?.command?.();
    expect(context.copy).toHaveBeenNthCalledWith(1, '/proj/src/foo.py');
    expect(context.copy).toHaveBeenNthCalledWith(2, 'src/foo.py');
  });

  it('disables copy-absolute when the root is unknown', () => {
    const items = buildFileTreeMenu(makeContext({ rootPath: '' }));
    expect(items.find(item => item.label === 'Copy abs')?.disabled).toBe(true);
    expect(items.find(item => item.label === 'Copy rel')?.disabled).toBeFalsy();
  });

  it('fires add-to-chat from its entry', () => {
    const context = makeContext();
    buildFileTreeMenu(context)
      .find(item => item.label === 'Add to chat')
      ?.command?.();
    expect(context.addToChat).toHaveBeenCalledTimes(1);
  });
});

describe('buildFileTreeMenu — desktop build', () => {
  it('adds reveal and the system-app entry', () => {
    const labels = labelsOf(makeContext({ desktop: true }));
    expect(labels).toContain('Reveal');
    const openWith = buildFileTreeMenu(makeContext({ desktop: true })).find(item => item.label === 'Open with');
    expect(openWith?.items?.map(item => item.label)).toEqual(['Viewer', 'System']);
  });

  it('fires reveal from its entry', () => {
    const context = makeContext({ desktop: true });
    buildFileTreeMenu(context)
      .find(item => item.label === 'Reveal')
      ?.command?.();
    expect(context.reveal).toHaveBeenCalledTimes(1);
  });

  it('fires the system-app handler from the submenu', () => {
    const context = makeContext({ desktop: true });
    buildFileTreeMenu(context)
      .find(item => item.label === 'Open with')
      ?.items?.find(item => item.label === 'System')
      ?.command?.();
    expect(context.openSystem).toHaveBeenCalledTimes(1);
  });
});
