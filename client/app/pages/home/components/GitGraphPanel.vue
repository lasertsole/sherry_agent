<template>
  <div class="flex h-full min-h-0 flex-col">
    <!-- Header: current branch (or the detached HEAD) + the dirty count + refresh.
         The branch is also the switcher's trigger (VS Code's graph has the same
         dropdown): clicking it lists the local branches, the current one marked,
         and picking another checks it out. -->
    <div
      class="flex shrink-0 items-center gap-2 border-b border-solid border-gray-100 px-3 py-1.5 text-xs dark:border-gray-800">
      <i class="pi pi-sitemap text-theme-main"></i>
      <button
        type="button"
        class="flex min-w-0 items-center gap-1 rounded px-1 py-0.5 font-mono text-gray-500 hover:bg-gray-100 disabled:cursor-default disabled:hover:bg-transparent dark:text-gray-400 dark:hover:bg-gray-800/70"
        :disabled="branches.length === 0"
        :title="branches.length ? t('gitGraph.switchBranch') : branchLabel"
        :aria-label="t('gitGraph.switchBranch')"
        data-test="git-branch"
        @click="openBranchMenu($event)">
        <span class="truncate">{{ branchLabel }}</span>
        <span
          v-if="branches.length"
          class="pi pi-chevron-down shrink-0 text-[10px]"
          aria-hidden="true"></span>
      </button>
      <span
        v-if="page.available && page.dirty > 0"
        class="shrink-0 rounded-full bg-amber-100 px-2 py-0.5 text-[11px] text-amber-700 dark:bg-amber-900/40 dark:text-amber-300"
        data-test="git-dirty">
        {{ t('gitGraph.dirty', { count: page.dirty }) }}
      </span>
      <Button
        icon="pi pi-refresh"
        size="small"
        text
        severity="secondary"
        class="ml-auto"
        :title="t('gitGraph.refresh')"
        :aria-label="t('gitGraph.refresh')"
        data-test="git-refresh"
        @click="reload" />
    </div>

    <div class="min-h-0 flex-1 overflow-auto">
      <div
        v-if="loading && rows.length === 0"
        class="flex items-center justify-center py-8">
        <ProgressSpinner style="width: 1.5rem; height: 1.5rem" />
      </div>

      <!-- One empty state per refusal: a directory that is not a repository, a
           missing git binary, or a repository without commits. -->
      <p
        v-else-if="!page.available"
        class="m-0 break-all px-3 py-2 text-xs text-gray-400 dark:text-gray-500"
        data-test="git-unavailable">
        {{ unavailableText }}
      </p>

      <p
        v-else-if="rows.length === 0"
        class="m-0 px-3 py-2 text-xs text-gray-400 dark:text-gray-500"
        data-test="git-empty">
        {{ t('gitGraph.empty') }}
      </p>

      <template v-else>
        <div
          v-for="row in rows"
          :key="row.commit.hash"
          class="flex cursor-pointer items-center gap-1.5 px-2 hover:bg-gray-50 dark:hover:bg-gray-800/60"
          :style="{ height: `${ROW_HEIGHT}px` }"
          :class="{ 'bg-gray-100 dark:bg-gray-800/80': expanded === row.commit.hash }"
          :data-test="`git-commit-${row.commit.hash}`"
          :title="`${row.commit.subject}\n${row.commit.author} · ${compactDate(row.commit.date)}`"
          @click="toggleRow(row.commit.hash)"
          @contextmenu.prevent="openCommitMenu($event, row)">
          <!-- The lane cell: one SVG per row, drawn from the lane state entering
               and leaving that row (see graphRows — VS Code's own geometry). -->
          <svg
            class="shrink-0 overflow-visible"
            :width="row.width"
            :height="ROW_HEIGHT"
            :viewBox="`0 0 ${row.width} ${ROW_HEIGHT}`"
            aria-hidden="true">
            <path
              v-for="(segment, index) in row.paths"
              :key="index"
              :d="segment.d"
              fill="none"
              :stroke="segment.color"
              :stroke-width="NODE_STROKE"
              stroke-linecap="round" />
            <circle
              v-if="row.head"
              :cx="laneX(row.lane)"
              :cy="NODE_Y"
              :r="CIRCLE_RADIUS + 3"
              fill="none"
              :stroke="row.color"
              :stroke-width="NODE_STROKE" />
            <template v-if="row.merge">
              <circle
                :cx="laneX(row.lane)"
                :cy="NODE_Y"
                :r="CIRCLE_RADIUS + 2"
                class="fill-white dark:fill-[#1f1f28]"
                :stroke="row.color"
                :stroke-width="NODE_STROKE" />
              <circle
                :cx="laneX(row.lane)"
                :cy="NODE_Y"
                :r="CIRCLE_RADIUS - 1"
                :fill="row.color" />
            </template>
            <circle
              v-else
              :cx="laneX(row.lane)"
              :cy="NODE_Y"
              :r="CIRCLE_RADIUS + 1"
              :fill="row.color"
              class="stroke-white dark:stroke-[#1f1f28]"
              :stroke-width="NODE_STROKE" />
          </svg>
          <!-- Ref chips lead the row, like VS Code's SCM graph. -->
          <span
            v-for="ref in row.commit.refs"
            :key="`${ref.kind}:${ref.name}`"
            class="shrink-0 rounded px-1 font-mono text-[10px] leading-4"
            :class="refClass(ref.kind)"
            :data-test="`git-ref-${ref.kind}-${ref.name}`"
            @contextmenu.prevent.stop="openRefMenu($event, ref)">
            {{ ref.name }}
          </span>
          <span class="min-w-0 flex-1 truncate text-xs text-theme-main">{{ row.commit.subject }}</span>
          <span class="shrink-0 font-mono text-[10px] text-gray-400">{{ row.commit.short }}</span>
        </div>

        <!-- Expanded detail (click a row): the commit's metadata, then the files
             it touched — clicking a file opens its diff in a right-sidebar tab. -->
        <div
          v-if="expandedRow"
          class="flex flex-col gap-1 border-y border-solid border-gray-100 px-3 py-1.5 text-[11px] text-gray-500 dark:border-gray-800 dark:text-gray-400"
          data-test="git-detail">
          <span class="text-xs text-theme-main">{{ expandedRow.commit.subject }}</span>
          <span
            class="break-all font-mono"
            data-test="git-detail-hash">
            {{ expandedRow.commit.hash }}
          </span>
          <span v-if="expandedRow.commit.parents.length">
            {{ t('gitGraph.parents') }}:
            <span class="font-mono">
              {{ expandedRow.commit.parents.map(p => p.slice(0, 8)).join(', ') }}
            </span>
          </span>
          <span>{{ expandedRow.commit.author }} · {{ compactDate(expandedRow.commit.date) }}</span>

          <div
            v-if="filesState === 'loading'"
            class="flex items-center gap-2 py-1">
            <ProgressSpinner style="width: 1rem; height: 1rem" />
            <span>{{ t('gitGraph.loadingFiles') }}</span>
          </div>
          <p
            v-else-if="filesState === 'error'"
            class="m-0 text-red-500 dark:text-red-400"
            data-test="git-files-error">
            {{ t('gitGraph.filesFailed') }}
          </p>
          <template v-else>
            <div
              v-if="commitFiles.length === 0"
              class="text-gray-400"
              data-test="git-files-empty">
              {{ t('gitGraph.noFiles') }}
            </div>
            <button
              v-for="file in commitFiles"
              :key="`${file.status}:${file.path}`"
              type="button"
              class="flex w-full items-center gap-1.5 rounded px-1 py-0.5 text-left hover:bg-gray-100 dark:hover:bg-gray-800/70"
              :data-test="`git-file-${file.status}-${file.path}`"
              @click.stop="openDiff(expandedRow.commit, file)">
              <span
                class="w-4 shrink-0 rounded text-center font-mono text-[10px] leading-4"
                :class="statusClass(file.status)">
                {{ file.status }}
              </span>
              <span class="min-w-0 flex-1 truncate font-mono text-[11px] text-theme-main">
                {{ file.old_path ? `${file.old_path} → ${file.path}` : file.path }}
              </span>
            </button>
          </template>
        </div>

        <div
          v-if="page.hasMore"
          class="p-2">
          <Button
            :label="t('gitGraph.loadMore')"
            size="small"
            text
            severity="secondary"
            :loading="loading"
            class="w-full"
            data-test="git-load-more"
            @click="loadMore" />
        </div>
      </template>
    </div>

    <!-- Right-click menu: commit actions on a row, ref actions on a chip. The
         two reset actions go through PrimeVue's confirmation dialog. -->
    <ContextMenu
      ref="menuRef"
      :model="menuItems"
      data-test="git-context-menu" />
  </div>
</template>

<script lang="ts" setup>
import { computed, onMounted, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import type { GitCommitEntry, GitCommitFile, GitGraphPage, GitRefEntry } from '~/composables/bridge/git';
// Stable module specifiers so tests can vi.mock the bridge (the unimport
// injection is compile-time and leaves bare symbols unmockable).
/* eslint-disable @typescript-eslint/no-restricted-imports */
import { checkoutGitRef, fetchCommitFiles, fetchGitGraph, resetGitBranch } from '~/composables/bridge/git';
/* eslint-enable @typescript-eslint/no-restricted-imports */
import { logUtil } from '~/utils/log';

const { t } = useI18n({ useScope: 'local' });

const props = defineProps<{ sessionId: string }>();

// The graph geometry and palette are VS Code's own (scmHistory.ts):
// SWIMLANE_HEIGHT / SWIMLANE_WIDTH / SWIMLANE_CURVE_RADIUS / CIRCLE_RADIUS /
// CIRCLE_STROKE_WIDTH, and the five rotating scmGraph.foreground colours — so a
// history here reads like the SCM graph the user already knows.
/** Row height (SWIMLANE_HEIGHT) and the lane pitch (SWIMLANE_WIDTH). */
const ROW_HEIGHT = 22;
const LANE_WIDTH = 11;
/** Corner radius of a branch/merge curve. */
const CURVE_RADIUS = 5;
/** Node circle radius and the stroke width of lines and rings. */
const CIRCLE_RADIUS = 4;
const NODE_STROKE = 2;
/** y of the node inside its row (VS Code draws it at SWIMLANE_WIDTH). */
const NODE_Y = LANE_WIDTH;

/** The graph foreground rotation (VS Code's scmGraph.foreground1..5). */
const LANE_COLORS = ['#FFB000', '#DC267F', '#994F00', '#40B0A6', '#B66DFF'];

/** How many commits one page holds (the backend clamps harder). */
const PAGE_SIZE = 40;

/** Empty page: what the template renders before the first load settles. */
const EMPTY_PAGE: GitGraphPage = {
  root: '',
  source: '',
  available: true,
  reason: '',
  branch: '',
  detached: false,
  dirty: 0,
  dirty_capped: false,
  commits: [],
  hasMore: false,
  branches: []
};

const page = ref<GitGraphPage>({ ...EMPTY_PAGE });
/** hash → the commit's file list (loaded on the first expansion, then cached). */
const filesByHash = ref<Record<string, GitCommitFile[]>>({});
/** The expansion's file-list state: idle / loading / error (per open row). */
const filesState = ref<'idle' | 'loading' | 'error'>('idle');
const rightSidebar = useRightSidebarStore();
const gitDiff = useGitDiffStore();
const loading = ref(false);
const expanded = ref('');
const menuRef = ref<{ show: (event: Event) => void } | null>(null);
const confirm = useConfirm();

/** A lane being tracked: the hash it waits for and its branch colour. */
interface Lane {
  id: string;
  color: string;
}

/** One rendered row: the commit, the lane state around it and its drawing. */
interface GraphRow {
  commit: GitCommitEntry;
  /** Lane the commit's node sits in (its index in the row's input lanes). */
  lane: number;
  /** The node's colour. */
  color: string;
  /** SVG segments of the row, each with the lane colour it belongs to. */
  paths: Array<{ d: string; color: string }>;
  /** True for a merge (more than one parent) — a ring, like VS Code. */
  merge: boolean;
  /** True when the row carries the HEAD ref — an extra outer ring. */
  head: boolean;
  /** Lane cell width for this row. */
  width: number;
}

/**
 * Lane x for one lane index (VS Code: `SWIMLANE_WIDTH * (index + 1)`).
 * @param lane Lane index.
 */
const laneX = (lane: number): number => LANE_WIDTH * (lane + 1);

/**
 * A straight running lane through the row.
 * @param x
 */
const vertical = (x: number): string => `M ${x} 0 V ${ROW_HEIGHT}`;

/**
 * A lane that ENDS at this row's node (this commit is what it waited for).
 * @param x
 */
const intoNode = (x: number): string => `M ${x} 0 V ${NODE_Y}`;

/**
 * The S-curve a lane draws while its index shifts: two quarter arcs joined by a
 * horizontal run — the shape VS Code's renderer builds with
 * `A ${r} ${r} 0 0 1 … H … A ${r} ${r} 0 0 0 …` (the sweeps flip with the
 * direction, so an up-shift mirrors the down-shift).
 * @param fromX Lane x at the top of the row.
 * @param toX Lane x at the bottom.
 */
const shiftCurve = (fromX: number, toX: number): string => {
  const right = toX > fromX;
  const sweepOut = right ? 1 : 0;
  const sweepIn = right ? 0 : 1;
  const arcX = fromX + (right ? CURVE_RADIUS : -CURVE_RADIUS);
  const joinX = toX + (right ? -CURVE_RADIUS : CURVE_RADIUS);
  return (
    `M ${fromX} 0 A ${CURVE_RADIUS} ${CURVE_RADIUS} 0 0 ${sweepOut} ${arcX} ${CURVE_RADIUS}` +
    ` H ${joinX} A ${CURVE_RADIUS} ${CURVE_RADIUS} 0 0 ${sweepIn} ${toX} ${CURVE_RADIUS * 2}` +
    ` V ${ROW_HEIGHT}`
  );
};

/**
 * The merge edge: from the node down to the lane an extra parent just opened,
 * using the same two-arc form but starting at the node's y (VS Code's merge
 * line, which ends at `SWIMLANE_WIDTH * 2`).
 * @param fromX The node's lane x.
 * @param toX The parent's lane x.
 */
const mergeCurve = (fromX: number, toX: number): string => {
  const right = toX > fromX;
  const sweep = right ? 1 : 0;
  const arcX = fromX + (right ? CURVE_RADIUS : -CURVE_RADIUS);
  const joinX = toX + (right ? -CURVE_RADIUS : CURVE_RADIUS);
  return (
    `M ${fromX} ${NODE_Y} A ${CURVE_RADIUS} ${CURVE_RADIUS} 0 0 ${sweep} ${arcX} ${NODE_Y + CURVE_RADIUS}` +
    ` H ${joinX} A ${CURVE_RADIUS} ${CURVE_RADIUS} 0 0 ${sweep} ${toX} ${ROW_HEIGHT}`
  );
};

/**
 * Lay the commits into lanes and draw each row — the walk VS Code's
 * `toISCMHistoryItemViewModelArray` performs: each row's input lanes are the
 * previous row's output, the FIRST parent inherits the node's lane and colour,
 * and every extra parent opens a NEW lane at the right edge with the next colour
 * in the rotation (which is what keeps a branch's colour stable as lanes shift).
 * @param commits Page commits, newest first.
 * @returns The rows with their geometry.
 */
function graphRows(commits: GitCommitEntry[]): GraphRow[] {
  let lanes: Lane[] = [];
  let colorIndex = 0;
  const rows: GraphRow[] = [];

  for (const commit of commits) {
    const input = lanes.map(lane => ({ ...lane }));
    let lane = input.findIndex(entry => entry.id === commit.hash);
    if (lane === -1) {
      // A tip no lane was waiting for (a second branch head): it opens one.
      lane = input.length;
      input.push({ id: commit.hash, color: LANE_COLORS[colorIndex]! });
    }
    const color = input[lane]!.color;

    const output = input.map(entry => ({ ...entry }));
    output[lane] = { id: commit.parents[0] ?? '', color };
    for (const parent of commit.parents.slice(1)) {
      if (!parent || output.some(entry => entry.id === parent)) continue;
      colorIndex = (colorIndex + 1) % LANE_COLORS.length;
      output.push({ id: parent, color: LANE_COLORS[colorIndex]! });
    }
    const next = output.filter(entry => entry.id !== '');

    const paths: Array<{ d: string; color: string }> = [];
    input.forEach((entry, index) => {
      const target = output.findIndex(candidate => candidate.id === entry.id);
      const x = laneX(index);
      if (target === -1) {
        // The lane's expectation is this commit: it runs into the node row.
        paths.push({ d: intoNode(x), color: entry.color });
      } else if (target === index) {
        paths.push({ d: vertical(x), color: entry.color });
      } else {
        paths.push({ d: shiftCurve(x, laneX(target)), color: entry.color });
      }
    });
    // Lanes PAST the input's length are the extra parents this commit just
    // opened (the node's own lane continues into its first parent, so it is not
    // a new lane): each one gets a merge edge from the node.
    output.forEach((entry, index) => {
      if (index < input.length) return;
      paths.push({ d: mergeCurve(laneX(lane), laneX(index)), color: entry.color });
    });

    lanes = next;
    rows.push({
      commit,
      lane,
      color,
      paths,
      merge: commit.parents.length > 1,
      head: commit.refs.some(ref => ref.kind === 'head'),
      width: laneX(Math.max(input.length, next.length) - 1) + LANE_WIDTH
    });
  }
  return rows;
}

const rows = computed<GraphRow[]>(() => graphRows(page.value.commits));

/** The files of the expanded commit ([] until the list lands). */
const commitFiles = computed<GitCommitFile[]>(() => {
  const hash = expanded.value;
  return hash ? (filesByHash.value[hash] ?? []) : [];
});

/** The row whose detail block is open (undefined once a refresh drops it). */
const expandedRow = computed<GraphRow | undefined>(() => rows.value.find(row => row.commit.hash === expanded.value));

/** Branch text: the branch name, or a short hash for a detached HEAD. */
const branchLabel = computed<string>(() => {
  if (!page.value.available) return t('gitGraph.unavailableShort');
  if (page.value.branch && !page.value.detached) return page.value.branch;
  const first = page.value.commits[0];
  if (page.value.detached && first) return `HEAD @ ${first.short}`;
  return t('gitGraph.noCommits');
});

/** The local branches the switcher offers (empty for an unusable repository).
 *  Tolerant of a page without the field: an instance kept across an HMR update,
 *  or any older payload, must not take the whole panel's render down. */
const branches = computed<string[]>(() =>
  page.value.available && Array.isArray(page.value.branches) ? page.value.branches : []
);

/** The explanation shown when the project directory is not a repository. */
const unavailableText = computed<string>(() =>
  page.value.reason === 'git-unavailable' ? t('gitGraph.gitMissing') : t('gitGraph.notARepository')
);

/**
 * Chip classes per ref kind.
 * @param kind Ref kind from the backend.
 */
const refClass = (kind: string): string => {
  if (kind === 'head') return 'bg-[#c1d6e5] text-theme-main dark:bg-[#41556b] dark:text-gray-100';
  if (kind === 'tag') return 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300';
  if (kind === 'remote') return 'bg-gray-100 text-gray-500 dark:bg-gray-800 dark:text-gray-400';
  return 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300';
};

/**
 * Compact timestamp for a row (`YYYY-MM-DD HH:mm`).
 * @param iso ISO-8601 author date.
 */
const compactDate = (iso: string): string => {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const pad = (value: number) => String(value).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
};

/**
 * Chip tint for a status letter (`M` / `A` / `D` / `R` …).
 * @param status Git status letter.
 */
const statusClass = (status: string): string => {
  if (status === 'A') return 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300';
  if (status === 'D') return 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300';
  if (status === 'R' || status === 'C') {
    return 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300';
  }
  return 'bg-gray-100 text-gray-500 dark:bg-gray-800 dark:text-gray-400';
};

/**
 * Toggle a row's detail block, loading the commit's file list on first open.
 * @param hash Commit hash of the clicked row.
 */
const toggleRow = (hash: string): void => {
  if (expanded.value === hash) {
    expanded.value = '';
    filesState.value = 'idle';
    return;
  }
  expanded.value = hash;
  void loadFiles(hash);
};

/**
 * Fetch one commit's file list (idempotent; a cached list renders at once).
 * @param hash Commit hash.
 */
const loadFiles = async (hash: string): Promise<void> => {
  if (filesByHash.value[hash]) {
    filesState.value = 'idle';
    return;
  }
  filesState.value = 'loading';
  try {
    const detail = await fetchCommitFiles(props.sessionId, hash);
    filesByHash.value = { ...filesByHash.value, [hash]: detail.files };
    filesState.value = 'idle';
  } catch (e) {
    logUtil.e('[GitGraphPanel] Failed to load the commit files:', e);
    filesState.value = 'error';
  }
};

/**
 * Open one file's diff from a commit: the panel shows it, and the store keeps the
 * target so the tab survives a remount (the sidebar has no KeepAlive).
 * @param commit The commit the file belongs to.
 * @param file The clicked file.
 */
const openDiff = (commit: GitCommitEntry, file: GitCommitFile): void => {
  gitDiff.open({
    sessionId: props.sessionId,
    hash: commit.hash,
    short: commit.short,
    path: file.path,
    subject: commit.subject
  });
  rightSidebar.openTab('gitDiff', { path: file.path, hash: commit.hash });
};

/**
 * Copy a value and confirm it with a toast.
 * @param text Value to copy.
 * @param label What was copied (named in the toast).
 */
const copyAndToast = async (text: string, label: string): Promise<void> => {
  const ok = await copyTextToClipboard(text);
  if (ok) toastSuccess(t('gitGraph.copied', { what: label }));
  else toastError(t('gitGraph.copyFailed'));
};

/**
 * Run a write action (reset / checkout) and repaint from its answer.
 *
 * The action THROWS on a refusal (the bridge turns the server's `reason` into the
 * error message), so the page is never assigned a null — and the toast shows
 * git's own words when it has any (a blocked checkout names the files).
 * @param action The bridge call that performs it.
 * @param failure Toast text for a refusal.
 */
const runAction = async (action: () => Promise<GitGraphPage>, failure: string): Promise<void> => {
  loading.value = true;
  try {
    page.value = await action();
    expanded.value = '';
  } catch (e) {
    logUtil.e('[GitGraphPanel] git action failed:', e);
    const reason = e instanceof Error ? e.message : '';
    toastError(failure, reason && reason !== failure ? reason : undefined);
  } finally {
    loading.value = false;
  }
};

/**
 * Move the current branch (the 回退 actions), behind a confirmation dialog.
 * @param row The right-clicked row.
 * @param mode `soft` keeps the changes staged, `hard` discards them.
 */
const resetTo = (row: GraphRow, mode: 'soft' | 'hard'): void => {
  const hard = mode === 'hard';
  confirm.require({
    header: hard ? t('gitGraph.confirmHardTitle') : t('gitGraph.confirmSoftTitle'),
    message: hard
      ? t('gitGraph.confirmHardMessage', {
          branch: page.value.branch || 'HEAD',
          hash: row.commit.short,
          count: page.value.dirty
        })
      : t('gitGraph.confirmSoftMessage', {
          branch: page.value.branch || 'HEAD',
          hash: row.commit.short
        }),
    acceptProps: {
      label: t('gitGraph.resetAction', {
        mode: hard ? t('gitGraph.modeHard') : t('gitGraph.modeSoft')
      }),
      severity: hard ? 'danger' : 'primary',
      icon: 'pi pi-history'
    },
    rejectProps: { label: t('common.cancel'), severity: 'secondary' },
    accept: () => {
      void runAction(
        () => resetGitBranch(props.sessionId, row.commit.hash, hard ? 'hard' : 'soft'),
        t('gitGraph.resetFailed')
      );
    }
  });
};

/** Items the ContextMenu renders (rebuilt per right-click). */
const menuItems = ref<Array<Record<string, unknown>>>([]);

/**
 * Open the context menu for a commit row.
 * @param event The contextmenu event (anchors the popup).
 * @param row The right-clicked row.
 */
const openCommitMenu = (event: Event, row: GraphRow): void => {
  menuItems.value = [
    {
      label: t('gitGraph.copyHash'),
      icon: 'pi pi-hashtag',
      command: () => void copyAndToast(row.commit.hash, t('gitGraph.copyHash'))
    },
    {
      label: t('gitGraph.copySubject'),
      icon: 'pi pi-copy',
      command: () => void copyAndToast(row.commit.subject, t('gitGraph.copySubject'))
    },
    { separator: true },
    {
      label: t('gitGraph.menuResetSoft'),
      icon: 'pi pi-history',
      command: () => resetTo(row, 'soft')
    },
    {
      label: t('gitGraph.menuResetHard'),
      icon: 'pi pi-exclamation-triangle',
      command: () => resetTo(row, 'hard')
    }
  ];
  menuRef.value?.show(event);
};

/**
 * Open the context menu for a ref chip: a branch can be checked out, every chip
 * can be copied.
 * @param event The contextmenu event (anchors the popup).
 * @param ref The right-clicked ref chip.
 */
const openRefMenu = (event: Event, ref: GitRefEntry): void => {
  const items: Array<Record<string, unknown>> = [
    {
      label: t('gitGraph.copyRef'),
      icon: 'pi pi-copy',
      command: () => void copyAndToast(ref.name, t('gitGraph.copyRef'))
    }
  ];
  if (ref.kind === 'branch' || ref.kind === 'head') {
    items.unshift({
      label: t('gitGraph.checkoutBranch'),
      icon: 'pi pi-sign-in',
      command: () => void runAction(() => checkoutGitRef(props.sessionId, ref.name), t('gitGraph.checkoutFailed'))
    });
  }
  menuItems.value = items;
  menuRef.value?.show(event);
};

/**
 * Open the branch switcher: every local branch, the current one ticked and
 * disabled, the rest switching on click (VS Code's graph dropdown switches
 * without a confirmation dialog; ``git`` itself refuses a checkout that would
 * clobber local changes, and that refusal surfaces as the failure toast).
 * @param event The click on the branch label (anchors the popup).
 */
const openBranchMenu = (event: Event): void => {
  const current = page.value.detached ? '' : page.value.branch;
  menuItems.value = branches.value.map(name => ({
    label: name,
    icon: name === current ? 'pi pi-check' : 'pi pi-code-branch',
    disabled: name === current,
    command: () => void runAction(() => checkoutGitRef(props.sessionId, name), t('gitGraph.checkoutFailed'))
  }));
  menuRef.value?.show(event);
};

/** (Re)load the first page. */
const reload = async (): Promise<void> => {
  if (!props.sessionId) return;
  loading.value = true;
  expanded.value = '';
  try {
    page.value = await fetchGitGraph(props.sessionId, { limit: PAGE_SIZE, skip: 0 });
  } catch (e) {
    logUtil.e('[GitGraphPanel] Failed to load the git graph:', e);
    page.value = { ...EMPTY_PAGE, available: false, reason: 'not-a-repository' };
  } finally {
    loading.value = false;
  }
};

/** Append the next page. */
const loadMore = async (): Promise<void> => {
  if (!props.sessionId || loading.value) return;
  loading.value = true;
  try {
    const next = await fetchGitGraph(props.sessionId, {
      limit: PAGE_SIZE,
      skip: page.value.commits.length
    });
    page.value = { ...next, commits: [...page.value.commits, ...next.commits] };
  } catch (e) {
    logUtil.e('[GitGraphPanel] Failed to load more commits:', e);
  } finally {
    loading.value = false;
  }
};

onMounted(() => {
  void reload();
});
// A different session (or the same one after its project directory changed)
// re-reads the history — the panel is mounted inside the sidebar's 工作目录 body.
watch(
  () => props.sessionId,
  () => {
    void reload();
  }
);
</script>

<i18n lang="json">
{
  "zh": {
    "gitGraph": {
      "refresh": "刷新",
      "dirty": "未提交 {count}",
      "loadMore": "加载更多",
      "empty": "暂无提交",
      "noCommits": "无提交",
      "notARepository": "当前工作目录不是 Git 仓库",
      "gitMissing": "服务器上没有找到 git 可执行文件",
      "unavailableShort": "不可用",
      "parents": "父提交",
      "loadingFiles": "读取文件列表…",
      "filesFailed": "读取文件列表失败",
      "noFiles": "该提交没有文件变更",
      "copied": "已复制{what}",
      "copyFailed": "复制失败",
      "copyHash": "复制提交哈希",
      "copySubject": "复制提交信息",
      "copyRef": "复制引用名",
      "switchBranch": "切换分支",
      "checkoutBranch": "切换到此分支",
      "checkoutFailed": "切换分支失败",
      "menuResetSoft": "软回退到此提交（保留改动）",
      "menuResetHard": "硬回退到此提交（丢弃改动）",
      "resetAction": "{mode}回退",
      "modeSoft": "软",
      "modeHard": "硬",
      "resetFailed": "回退失败",
      "confirmSoftTitle": "软回退分支",
      "confirmSoftMessage": "把 {branch} 移动到 {hash}？已提交的更改会回到暂存区，工作区内容保留。",
      "confirmHardTitle": "硬回退分支（危险）",
      "confirmHardMessage": "把 {branch} 移动到 {hash}？当前 {count} 项未提交改动会被丢弃，无法恢复。"
    }
  },
  "en": {
    "gitGraph": {
      "refresh": "Refresh",
      "dirty": "{count} uncommitted",
      "loadMore": "Load more",
      "empty": "No commits yet",
      "noCommits": "no commits",
      "notARepository": "The working directory is not a Git repository",
      "gitMissing": "No git executable on the server",
      "unavailableShort": "unavailable",
      "parents": "Parents",
      "loadingFiles": "Loading the file list…",
      "filesFailed": "Failed to load the file list",
      "noFiles": "This commit changes no files",
      "copied": "Copied {what}",
      "copyFailed": "Copy failed",
      "copyHash": "Copy commit hash",
      "copySubject": "Copy commit message",
      "copyRef": "Copy ref name",
      "switchBranch": "Switch branch",
      "checkoutBranch": "Check out this branch",
      "checkoutFailed": "Checkout failed",
      "menuResetSoft": "Reset here — soft (keep changes)",
      "menuResetHard": "Reset here — hard (discard changes)",
      "resetAction": "Reset ({mode})",
      "modeSoft": "soft",
      "modeHard": "hard",
      "resetFailed": "Reset failed",
      "confirmSoftTitle": "Soft-reset the branch",
      "confirmSoftMessage": "Move {branch} to {hash}? The committed changes return to the index and the working tree keeps its files.",
      "confirmHardTitle": "Hard-reset the branch (dangerous)",
      "confirmHardMessage": "Move {branch} to {hash}? {count} uncommitted change(s) will be discarded and cannot be recovered."
    }
  },
  "ja": {
    "gitGraph": {
      "refresh": "更新",
      "dirty": "未コミット {count}",
      "loadMore": "さらに読み込む",
      "empty": "コミットがまだありません",
      "noCommits": "コミットなし",
      "notARepository": "作業ディレクトリは Git リポジトリではありません",
      "gitMissing": "サーバーに git 実行ファイルが見つかりません",
      "unavailableShort": "利用不可",
      "parents": "親コミット",
      "loadingFiles": "ファイル一覧を読み込み中…",
      "filesFailed": "ファイル一覧の読み込みに失敗しました",
      "noFiles": "このコミットにファイル変更はありません",
      "copied": "{what}をコピーしました",
      "copyFailed": "コピーに失敗しました",
      "copyHash": "コミット ハッシュをコピー",
      "copySubject": "コミット メッセージをコピー",
      "copyRef": "参照名をコピー",
      "switchBranch": "ブランチを切り替え",
      "checkoutBranch": "このブランチに切り替え",
      "checkoutFailed": "ブランチの切り替えに失敗しました",
      "menuResetSoft": "ここへソフト リセット（変更を保持）",
      "menuResetHard": "ここへハード リセット（変更を破棄）",
      "resetAction": "{mode} リセット",
      "modeSoft": "ソフト",
      "modeHard": "ハード",
      "resetFailed": "リセットに失敗しました",
      "confirmSoftTitle": "ブランチをソフト リセット",
      "confirmSoftMessage": "{branch} を {hash} に移動しますか？コミット済みの変更はステージに戻り、作業ツリーは保持されます。",
      "confirmHardTitle": "ブランチをハード リセット（危険）",
      "confirmHardMessage": "{branch} を {hash} に移動しますか？未コミットの {count} 件の変更は破棄され、復元できません。"
    }
  },
  "ko": {
    "gitGraph": {
      "refresh": "새로고침",
      "dirty": "미커밋 {count}",
      "loadMore": "더 불러오기",
      "empty": "아직 커밋이 없습니다",
      "noCommits": "커밋 없음",
      "notARepository": "작업 디렉터리가 Git 저장소가 아닙니다",
      "gitMissing": "서버에 git 실행 파일이 없습니다",
      "unavailableShort": "사용 불가",
      "parents": "부모 커밋",
      "loadingFiles": "파일 목록 불러오는 중…",
      "filesFailed": "파일 목록을 불러오지 못했습니다",
      "noFiles": "이 커밋에는 파일 변경이 없습니다",
      "copied": "{what} 복사됨",
      "copyFailed": "복사 실패",
      "copyHash": "커밋 해시 복사",
      "copySubject": "커밋 메시지 복사",
      "copyRef": "참조 이름 복사",
      "switchBranch": "브랜치 전환",
      "checkoutBranch": "이 브랜치로 전환",
      "checkoutFailed": "브랜치 전환 실패",
      "menuResetSoft": "여기로 소프트 리셋(변경 유지)",
      "menuResetHard": "여기로 하드 리셋(변경 버림)",
      "resetAction": "{mode} 리셋",
      "modeSoft": "소프트",
      "modeHard": "하드",
      "resetFailed": "리셋 실패",
      "confirmSoftTitle": "브랜치 소프트 리셋",
      "confirmSoftMessage": "{branch}을(를) {hash}로 이동할까요? 커밋된 변경은 스테이지로 돌아가고 작업 트리는 유지됩니다.",
      "confirmHardTitle": "브랜치 하드 리셋(위험)",
      "confirmHardMessage": "{branch}을(를) {hash}로 이동할까요? 미커밋 변경 {count}건이 버려지고 복구할 수 없습니다."
    }
  }
}
</i18n>
