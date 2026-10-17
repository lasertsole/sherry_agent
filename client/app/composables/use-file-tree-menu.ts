/**
 * The project file tree's right-click menu (ZCode's workspace-file-tree rows).
 *
 * Seven entries over three capability tiers: pure frontend (Open, Copy
 * absolute / relative path, Add to chat), runtime-gated (Reveal in the file
 * manager, Open with the system application — desktop build only, a browser
 * cannot reach the OS), and the built-in browser (HTML files; the desktop
 * build loads them through Tauri's asset protocol, the web build through a
 * blob URL). The tree stays READ-ONLY: no new/rename/delete, exactly like
 * ZCode's rows and Sherry's `GET /project/tree` contract.
 *
 * @module composables/use-file-tree-menu
 */
import type { FileMentionPayload } from './use-file-mention';

/** One menu entry (PrimeVue `MenuItem`'s shape, kept local to avoid the dep). */
export interface FileTreeMenuItem {
  label?: string;
  icon?: string;
  disabled?: boolean;
  separator?: boolean;
  items?: FileTreeMenuItem[];
  command?: () => void;
}

/** What a menu build needs to know about the right-clicked row. */
export interface FileTreeMenuNode {
  type: 'file' | 'dir';
  /** Path relative to the project root. */
  path: string;
  /** The entry's own name. */
  name: string;
}

/** The handlers + context a menu build closes over. */
export interface FileTreeMenuContext {
  node: FileTreeMenuNode;
  /** The session whose project this tree belongs to. */
  sessionId: string;
  /** The session's project root (absolute); '' when unknown. */
  rootPath: string;
  /** Labels (already translated by the caller). */
  labels: {
    open: string;
    openWith: string;
    openInViewer: string;
    openWithSystem: string;
    openInBrowser: string;
    reveal: string;
    copyAbsolutePath: string;
    copyRelativePath: string;
    addToChat: string;
  };
  /** Whether the desktop-only entries are available in this runtime. */
  desktop: boolean;
  /** Open the row (file → viewer tab, dir → expand/collapse). */
  open: () => void;
  /** Open the file with the OS default application (desktop only). */
  openSystem: () => void;
  /** Open the file in the built-in browser panel. */
  openBrowser: () => void;
  /** Reveal the row in the OS file manager (desktop only). */
  reveal: () => void;
  /** Copy a string to the clipboard (the caller toasts the result). */
  copy: (text: string) => void;
  /** Insert an `@path` mention into the session's composer. */
  addToChat: () => void;
}

/**
 * Whether a row can be opened in the built-in browser (a local HTML page).
 * @param node
 */
export function canOpenInBuiltInBrowser(node: FileTreeMenuNode): boolean {
  if (node.type !== 'file') return false;
  const lower = node.path.toLowerCase();
  return lower.endsWith('.html') || lower.endsWith('.htm');
}

/**
 * The absolute path of a tree row: the session's root joined with the relative
 * path. Empty when the root is unknown (an unbound session never renders the
 * tree, so this is a safety net, not a state).
 * @param rootPath The session's project root.
 * @param relativePath The row's path relative to that root.
 */
export function absolutePathOf(rootPath: string, relativePath: string): string {
  if (!rootPath) return '';
  const sep = rootPath.includes('\\') && !rootPath.includes('/') ? '\\' : '/';
  if (relativePath.includes('\\') || relativePath.includes('/')) {
    const parts = relativePath.split(/[\\/]+/).filter(Boolean);
    return [rootPath.replace(/[\\/]+$/, ''), ...parts].join(sep);
  }
  return `${rootPath.replace(/[\\/]+$/, '')}${sep}${relativePath}`;
}

/**
 * Build the row's menu.
 * @param context The row, its session and every handler the menu fires.
 */
export function buildFileTreeMenu(context: FileTreeMenuContext): FileTreeMenuItem[] {
  const { node, labels, desktop } = context;
  const isFile = node.type === 'file';
  const items: FileTreeMenuItem[] = [
    {
      label: labels.open,
      icon: 'pi pi-fw pi-external-link',
      command: context.open
    }
  ];

  if (isFile) {
    const submenu: FileTreeMenuItem[] = [
      {
        label: labels.openInViewer,
        icon: 'pi pi-file',
        command: context.open
      }
    ];
    if (desktop) {
      submenu.push({
        label: labels.openWithSystem,
        icon: 'pi pi-desktop',
        command: context.openSystem
      });
    }
    if (canOpenInBuiltInBrowser(node)) {
      submenu.push({
        label: labels.openInBrowser,
        icon: 'pi pi-globe',
        command: context.openBrowser
      });
    }
    items.push({
      label: labels.openWith,
      icon: 'pi pi-fw pi-external-link',
      items: submenu
    });
  }

  if (canOpenInBuiltInBrowser(node)) {
    items.push({
      label: labels.openInBrowser,
      icon: 'pi pi-fw pi-globe',
      command: context.openBrowser
    });
  }

  items.push({ separator: true });

  if (desktop) {
    items.push({
      label: labels.reveal,
      icon: 'pi pi-fw pi-folder-open',
      command: context.reveal
    });
  }

  items.push(
    {
      label: labels.copyAbsolutePath,
      icon: 'pi pi-fw pi-copy',
      disabled: !context.rootPath,
      command: () => context.copy(absolutePathOf(context.rootPath, node.path))
    },
    {
      label: labels.copyRelativePath,
      icon: 'pi pi-fw pi-copy',
      command: () => context.copy(node.path)
    },
    { separator: true },
    {
      label: labels.addToChat,
      icon: 'pi pi-fw pi-comment',
      command: context.addToChat
    }
  );

  return items;
}

/**
 * The mention payload for a row (what "Add to chat" inserts).
 * @param node The right-clicked row.
 */
export function mentionPayloadOf(node: FileTreeMenuNode): FileMentionPayload {
  return { type: node.type, path: node.path, name: node.name };
}
