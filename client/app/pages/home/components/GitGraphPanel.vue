<template>
  <div class="flex h-full min-h-0 flex-col">
    <!-- Header: current branch (or the detached HEAD) + the dirty count + refresh. -->
    <div
      class="flex shrink-0 items-center gap-2 border-b border-solid border-gray-100 px-3 py-2 text-xs dark:border-gray-800">
      <i class="pi pi-sitemap text-theme-main"></i>
      <span
        class="truncate font-mono text-gray-500 dark:text-gray-400"
        data-test="git-branch"
        >{{ branchLabel }}</span
      >
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
          class="cursor-pointer px-2 py-1 hover:bg-gray-50 dark:hover:bg-gray-800/60"
          :class="{ 'bg-gray-50 dark:bg-gray-800/60': expanded === row.commit.hash }"
          :data-test="`git-commit-${row.commit.hash}`"
          :title="row.commit.subject"
          @click="toggleRow(row.commit.hash)">
          <div class="flex items-center gap-2">
            <!-- The lane cell: one SVG per row, drawn from the lane state
                 entering and leaving that row (see graphRows). -->
            <svg
              class="shrink-0"
              :width="row.width"
              :height="ROW_HEIGHT"
              :viewBox="`0 0 ${row.width} ${ROW_HEIGHT}`"
              aria-hidden="true">
              <path
                v-for="(d, index) in row.paths"
                :key="index"
                :d="d"
                fill="none"
                :stroke="laneColor(row.pathColors[index] ?? 0)"
                stroke-width="1.5"
                stroke-linecap="round" />
              <circle
                :cx="laneX(row.lane)"
                :cy="ROW_HEIGHT / 2"
                r="3.5"
                :fill="laneColor(row.lane)"
                stroke="white"
                stroke-width="1" />
            </svg>
            <div class="min-w-0 flex-1">
              <div class="truncate text-xs text-theme-main">{{ row.commit.subject }}</div>
              <div class="flex min-w-0 items-center gap-1.5 text-[11px] text-gray-400">
                <span class="shrink-0 font-mono">{{ row.commit.short }}</span>
                <span class="truncate">{{ row.commit.author }}</span>
                <span class="ml-auto shrink-0">{{ compactDate(row.commit.date) }}</span>
              </div>
            </div>
          </div>

          <!-- Ref chips (branch / tag / remote / HEAD) sit under the row so a
               long subject keeps the full width. -->
          <div
            v-if="row.commit.refs.length > 0"
            class="mt-0.5 flex flex-wrap gap-1 pl-1">
            <span
              v-for="ref in row.commit.refs"
              :key="`${ref.kind}:${ref.name}`"
              class="rounded-full px-1.5 py-0.5 font-mono text-[10px]"
              :class="refClass(ref.kind)">
              {{ ref.name }}
            </span>
          </div>

          <!-- Expanded detail: the full hash, the parents and the timestamp. -->
          <div
            v-if="expanded === row.commit.hash"
            class="mt-1 flex flex-col gap-0.5 rounded border border-solid border-gray-100 p-1.5 text-[11px] text-gray-500 dark:border-gray-800 dark:text-gray-400"
            data-test="git-detail">
            <span class="break-all font-mono">{{ row.commit.hash }}</span>
            <span v-if="row.commit.parents.length">
              {{ t('gitGraph.parents') }}:
              <span class="font-mono">{{ row.commit.parents.map(p => p.slice(0, 8)).join(', ') }}</span>
            </span>
            <span>{{ row.commit.author }} · {{ compactDate(row.commit.date) }}</span>
          </div>
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
  </div>
</template>

<script lang="ts" setup>
import { computed, onMounted, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import type { GitCommitEntry, GitGraphPage } from '~/composables/bridge/git';
// Stable module specifier so tests can vi.mock the bridge (the unimport
// injection is compile-time and leaves bare symbols unmockable).
/* eslint-disable @typescript-eslint/no-restricted-imports */
import { fetchGitGraph } from '~/composables/bridge/git';
/* eslint-enable @typescript-eslint/no-restricted-imports */
import { logUtil } from '~/utils/log';

const { t } = useI18n({ useScope: 'local' });

const props = defineProps<{ sessionId: string }>();

/** Row height in px (the SVG's viewBox height; the row's own height follows it). */
const ROW_HEIGHT = 34;
/** Lane pitch and the left gutter of the lane cell. */
const LANE_WIDTH = 14;
const LANE_GUTTER = 6;

/** Lane colours, indexed by lane (a small palette; VS Code-ish hues). */
const LANE_COLORS = ['#4e79a7', '#f28e2b', '#59a14f', '#e15759', '#b07aa1', '#76b7b2', '#edc948', '#ff9da7'];

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
  hasMore: false
};

const page = ref<GitGraphPage>({ ...EMPTY_PAGE });
const loading = ref(false);
const expanded = ref('');

/** One rendered row: the commit plus the lane geometry computed for it. */
interface GraphRow {
  commit: GitCommitEntry;
  /** Lane the commit's node sits in. */
  lane: number;
  /** Lane count of the row (its SVG width follows it). */
  laneCount: number;
  /** SVG path data per drawn segment, with the lane each one belongs to. */
  paths: string[];
  pathColors: number[];
  width: number;
}

/**
 * Lane x for one lane index.
 * @param lane Lane index.
 */
const laneX = (lane: number): number => LANE_GUTTER + lane * LANE_WIDTH;

/**
 * Colour for one lane.
 * @param lane Lane index.
 */
const laneColor = (lane: number): string => LANE_COLORS[lane % LANE_COLORS.length]!;

/**
 * Lay the commits out into lanes — the classic walk: each lane holds the hash it
 * is still waiting for, a commit takes the lane that expects it (or opens a new
 * one at the right edge), its first parent inherits that lane and extra parents
 * (a merge) open their own. The row's SVG is then drawn from the lane state
 * entering and leaving it, so a branch and a merge both read as lines.
 * @param commits Page commits, newest first.
 * @returns The rows with their geometry.
 */
function graphRows(commits: GitCommitEntry[]): GraphRow[] {
  let lanes: string[] = [];
  const rows: GraphRow[] = [];

  for (const commit of commits) {
    const before = [...lanes];
    let lane = before.indexOf(commit.hash);
    if (lane === -1) {
      // A tip no lane was waiting for (a second branch head): open a new lane.
      lane = before.length;
      before.push(commit.hash);
    }

    // The state LEAVING the row: the first parent continues in this lane, every
    // other parent gets a lane of its own right after it (deduplicated), and a
    // lane whose expectation is exhausted is dropped.
    const after = [...before];
    after[lane] = commit.parents[0] ?? '';
    let insertAt = lane + 1;
    for (const parent of commit.parents.slice(1)) {
      if (parent && !after.includes(parent)) {
        after.splice(insertAt, 0, parent);
        insertAt += 1;
      }
    }
    lanes = after.filter(name => name !== '');

    const laneCount = Math.max(before.length, after.length);
    const mid = ROW_HEIGHT / 2;
    const paths: string[] = [];
    const pathColors: number[] = [];
    for (let index = 0; index < laneCount; index += 1) {
      const above = index < before.length;
      const below = index < after.length;
      const x = laneX(index);
      if (above && below) {
        paths.push(`M ${x} 0 L ${x} ${ROW_HEIGHT}`);
        pathColors.push(index);
      } else if (above) {
        // Ends here: this commit is what the lane was waiting for.
        paths.push(`M ${x} 0 L ${x} ${mid}`);
        pathColors.push(index);
      } else if (below) {
        // Starts here (a new tip, or a merge parent): drop from the node's row.
        paths.push(`M ${x} ${mid} L ${x} ${ROW_HEIGHT}`);
        pathColors.push(index);
      }
    }
    // Merge edges: from the node to each extra parent's lane.
    for (const parent of commit.parents.slice(1)) {
      const target = after.indexOf(parent);
      if (target === -1 || target === lane) continue;
      paths.push(
        `M ${laneX(lane)} ${mid} C ${laneX(lane)} ${ROW_HEIGHT}, ${laneX(target)} ${ROW_HEIGHT}, ${laneX(target)} ${mid}`
      );
      pathColors.push(target);
    }

    rows.push({
      commit,
      lane,
      laneCount,
      paths,
      pathColors,
      width: laneX(laneCount) + LANE_GUTTER
    });
  }
  return rows;
}

const rows = computed<GraphRow[]>(() => graphRows(page.value.commits));

/** Branch text: the branch name, or a short hash for a detached HEAD. */
const branchLabel = computed<string>(() => {
  if (!page.value.available) return t('gitGraph.unavailableShort');
  if (page.value.branch && !page.value.detached) return page.value.branch;
  const first = page.value.commits[0];
  if (page.value.detached && first) return `HEAD @ ${first.short}`;
  return t('gitGraph.noCommits');
});

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
 * Toggle a row's detail block.
 * @param hash Commit hash of the clicked row.
 */
const toggleRow = (hash: string): void => {
  expanded.value = expanded.value === hash ? '' : hash;
};

/** (Re)load the first page. */
const reload = async (): Promise<void> => {
  if (!props.sessionId) return;
  loading.value = true;
  expanded.value = '';
  try {
    page.value = await fetchGitGraph(props.sessionId, { limit: 40, skip: 0 });
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
      limit: 40,
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
      "parents": "父提交"
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
      "parents": "Parents"
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
      "parents": "親コミット"
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
      "parents": "부모 커밋"
    }
  }
}
</i18n>
