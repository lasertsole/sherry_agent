import { defineStore } from 'pinia';

/** One file diff opened from the git graph. */
export interface GitDiffTarget {
  /** Session whose project directory the diff belongs to. */
  sessionId: string;
  /** Commit the diff was opened from. */
  hash: string;
  /** Short hash, for the tab strip. */
  short: string;
  /** File path inside that commit (its NEW path for a rename). */
  path: string;
  /** The commit's subject, shown in the panel header. */
  subject: string;
}

/**
 * The diffs the git graph opened, as a small MRU list.
 *
 * `RightSidebar` has no KeepAlive, so a panel remounts whenever the user
 * switches tabs — this store is what lets the git-diff tab come back to the file
 * it was showing (and lets several files from one commit stay as tabs inside the
 * panel). Keeping the `sessionId` on each target means the diff can never be
 * rendered against another session's project directory.
 */
export const useGitDiffStore = defineStore('gitDiff', () => {
  /** Opened diffs, oldest first (the last one is the active tab). */
  const targets = ref<GitDiffTarget[]>([]);
  /** Index of the tab the panel shows. */
  const activeIndex = ref(0);

  /** Cap on open diffs (each one is a click away again). */
  const MAX_TARGETS = 8;

  /**
   * Open (or re-activate) one file diff.
   * @param target The commit + file to show.
   */
  function open(target: GitDiffTarget): void {
    const existing = targets.value.findIndex(item => item.hash === target.hash && item.path === target.path);
    if (existing !== -1) {
      activeIndex.value = existing;
      return;
    }
    const next = [...targets.value, target];
    targets.value = next.length > MAX_TARGETS ? next.slice(next.length - MAX_TARGETS) : next;
    activeIndex.value = targets.value.length - 1;
  }

  /**
   * The diff currently shown.
   * @returns The active target, or null when nothing is open.
   */
  function active(): GitDiffTarget | null {
    return targets.value[activeIndex.value] ?? null;
  }

  /**
   * Show another opened diff.
   * @param index Tab index inside the panel.
   */
  function activate(index: number): void {
    if (index >= 0 && index < targets.value.length) activeIndex.value = index;
  }

  /**
   * Close one opened diff.
   * @param index Tab index inside the panel.
   */
  function close(index: number): void {
    targets.value = targets.value.filter((_item, position) => position !== index);
    activeIndex.value = Math.max(0, Math.min(activeIndex.value, targets.value.length - 1));
  }

  return { targets, activeIndex, open, active, activate, close };
});
