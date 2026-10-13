<template>
  <div class="flex h-full min-h-0 flex-col">
    <!-- The opened diffs, one tab each (several files of a commit stay open). -->
    <div
      v-if="targets.length > 1"
      class="flex shrink-0 flex-wrap items-center gap-1 border-b border-solid border-gray-100 px-2 py-1 dark:border-gray-800">
      <button
        v-for="(target, index) in targets"
        :key="`${target.hash}:${target.path}`"
        type="button"
        class="inline-flex max-w-[14rem] items-center gap-1 rounded px-2 py-0.5 font-mono text-[11px] transition-colors"
        :class="
          index === activeIndex
            ? 'bg-[#c1d6e5] text-theme-main dark:bg-[#41556b]'
            : 'bg-gray-100 text-gray-500 hover:bg-gray-200 dark:bg-gray-800 dark:text-gray-400'
        "
        :title="target.path"
        :data-test="`git-diff-tab-${target.short}-${target.path}`"
        @click="store.activate(index)">
        <span class="truncate">{{ target.path }}</span>
        <i
          class="pi pi-times text-[8px]"
          :data-test="`git-diff-tab-close-${target.short}-${target.path}`"
          @click.stop="store.close(index)" />
      </button>
    </div>

    <div
      v-if="!target"
      class="flex items-center justify-center py-8 text-xs text-gray-400">
      {{ t('gitDiff.empty') }}
    </div>

    <template v-else>
      <!-- Header: the file, its status and the two sides' labels. -->
      <div
        class="flex shrink-0 flex-wrap items-center gap-2 border-b border-solid border-gray-100 px-3 py-1.5 text-xs dark:border-gray-800">
        <span
          class="rounded px-1 font-mono text-[10px] leading-4"
          :class="statusClass(diff?.status ?? diffStatusFallback)"
          data-test="git-diff-status"
          >{{ diff?.status ?? '…' }}</span
        >
        <span
          class="min-w-0 truncate font-mono text-theme-main"
          data-test="git-diff-path">
          {{ diff?.path && diff.status === 'R' ? `${diff.old_path} → ${diff.path}` : target.path }}
        </span>
        <span
          class="ml-auto shrink-0 font-mono text-gray-400"
          data-test="git-diff-commit">
          {{ target.short }}
        </span>
        <button
          type="button"
          class="shrink-0 rounded px-1.5 py-0.5 text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800"
          :title="t('gitDiff.refresh')"
          data-test="git-diff-refresh"
          @click="load">
          <i class="pi pi-refresh text-[11px]" />
        </button>
      </div>

      <div
        v-if="loading"
        class="flex items-center justify-center py-8">
        <ProgressSpinner style="width: 1.5rem; height: 1.5rem" />
      </div>

      <!-- Non-text sides and over-long files say so instead of rendering junk. -->
      <p
        v-else-if="diff?.binary"
        class="m-0 px-3 py-2 text-xs text-gray-400 dark:text-gray-500"
        data-test="git-diff-binary">
        {{ t('gitDiff.binary') }}
      </p>
      <p
        v-else-if="diff?.notice === 'too-large'"
        class="m-0 px-3 py-2 text-xs text-gray-400 dark:text-gray-500"
        data-test="git-diff-too-large">
        {{ t('gitDiff.tooLarge') }}
      </p>

      <div
        v-else
        class="min-h-0 flex-1 overflow-auto"
        data-test="git-diff-body">
        <!-- Two columns when both sides exist (old left / new right — removed
             lines red on the left, added lines green on the right, in the same
             row); ONE full-width column for an added or deleted file. -->
        <table class="w-full border-collapse font-mono text-[11px] leading-4">
          <thead v-if="twoSides">
            <tr class="text-gray-400">
              <th
                class="w-10 border-b border-solid border-gray-100 px-1 py-0.5 text-right font-normal dark:border-gray-800">
                #
              </th>
              <th class="border-b border-solid border-gray-100 px-2 py-0.5 text-left font-normal dark:border-gray-800">
                {{ diff?.old_label }}
              </th>
              <th
                class="w-10 border-b border-l border-solid border-gray-100 px-1 py-0.5 text-right font-normal dark:border-gray-800">
                #
              </th>
              <th class="border-b border-solid border-gray-100 px-2 py-0.5 text-left font-normal dark:border-gray-800">
                {{ diff?.new_label }}
              </th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="(row, index) in rows"
              :key="index"
              :data-test="`git-diff-row-${index}`">
              <template v-if="twoSides">
                <td
                  class="w-10 select-none px-1 text-right align-top text-gray-400"
                  :class="cellClass(row.left)">
                  {{ row.left?.n ?? '' }}
                </td>
                <td
                  class="whitespace-pre px-2 align-top"
                  :class="cellClass(row.left)">
                  {{ row.left?.text ?? '' }}
                </td>
                <td
                  class="w-10 select-none border-l border-solid border-gray-100 px-1 text-right align-top text-gray-400 dark:border-gray-800"
                  :class="cellClass(row.right)">
                  {{ row.right?.n ?? '' }}
                </td>
                <td
                  class="whitespace-pre px-2 align-top"
                  :class="cellClass(row.right)">
                  {{ row.right?.text ?? '' }}
                </td>
              </template>
              <template v-else>
                <td
                  class="w-10 select-none px-1 text-right align-top text-gray-400"
                  :class="cellClass(singleSide)">
                  {{ singleSide?.n ?? '' }}
                </td>
                <td
                  class="whitespace-pre px-2 align-top"
                  :class="cellClass(singleSide)"
                  data-test="git-diff-single-cell">
                  {{ singleSide?.text ?? '' }}
                </td>
              </template>
            </tr>
          </tbody>
        </table>
        <p
          v-if="diff?.truncated"
          class="m-0 px-3 py-1 text-[11px] text-gray-400"
          data-test="git-diff-truncated">
          {{ t('gitDiff.truncated', { count: rows.length }) }}
        </p>
        <p
          v-if="rows.length === 0 && !diff?.binary"
          class="m-0 px-3 py-1 text-[11px] text-gray-400"
          data-test="git-diff-no-changes">
          {{ t('gitDiff.noChanges') }}
        </p>
      </div>
    </template>
  </div>
</template>

<script lang="ts" setup>
import { computed, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import type { GitCommitDiff, GitDiffLine } from '~/composables/bridge/git';
// Stable module specifier so tests can vi.mock the bridge (the unimport
// injection is compile-time and leaves bare symbols unmockable).
/* eslint-disable @typescript-eslint/no-restricted-imports */
import { fetchCommitDiff } from '~/composables/bridge/git';
/* eslint-enable @typescript-eslint/no-restricted-imports */
import { logUtil } from '~/utils/log';

const { t } = useI18n({ useScope: 'local' });

const store = useGitDiffStore();
const targets = computed(() => store.targets);
const activeIndex = computed(() => store.activeIndex);
const target = computed(() => store.active());

const diff = ref<GitCommitDiff | null>(null);
const loading = ref(false);
/** Status letter while the diff is still loading (the target knows nothing yet). */
const diffStatusFallback = 'M';

/** True when both sides exist (a modified/renamed file → two columns). */
const twoSides = computed(() => diff.value !== null && diff.value.status !== 'A' && diff.value.status !== 'D');
/** The side a single-column file renders (right for an add, left for a delete). */
const singleSide = computed<GitDiffLine | null>(() => {
  const rows = diff.value?.rows ?? [];
  for (const row of rows) {
    const side = row.left ?? row.right;
    if (side) return side;
  }
  return null;
});
const rows = computed(() => diff.value?.rows ?? []);

/**
 * Background tint for one cell: unchanged cells stay plain, a removed line is
 * red on the left and an added line green on the right (VS Code's colours).
 * @param line The cell's line (null for a filler half).
 */
const cellClass = (line: GitDiffLine | null | undefined): string => {
  if (!line) return 'bg-gray-50 dark:bg-gray-900/40';
  if (line.kind === 'remove') return 'bg-red-50 dark:bg-red-900/25';
  if (line.kind === 'add') return 'bg-emerald-50 dark:bg-emerald-900/25';
  return '';
};

/**
 * Chip tint for a status letter (`M` / `A` / `D` / `R` …).
 * @param status Git status letter.
 */
const statusClass = (status: string): string => {
  if (status === 'A') return 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300';
  if (status === 'D') return 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300';
  if (status === 'R' || status === 'C') return 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300';
  return 'bg-gray-100 text-gray-500 dark:bg-gray-800 dark:text-gray-400';
};

/** Fetch the active target's diff. */
const load = async (): Promise<void> => {
  const current = target.value;
  if (!current) {
    diff.value = null;
    return;
  }
  loading.value = true;
  try {
    diff.value = await fetchCommitDiff(current.sessionId, current.hash, current.path);
  } catch (e) {
    logUtil.e('[GitDiffPanel] Failed to load the commit diff:', e);
    diff.value = null;
  } finally {
    loading.value = false;
  }
};

// A different target (another file, another commit) reloads; the tab strip
// inside the panel switches between the opened targets without a fetch storm.
watch(
  () => `${target.value?.sessionId ?? ''}:${target.value?.hash ?? ''}:${target.value?.path ?? ''}`,
  () => {
    void load();
  },
  { immediate: true }
);
</script>

<i18n lang="json">
{
  "zh": {
    "gitDiff": {
      "empty": "从 Git Graph 点开某个提交的文件后，修改内容会显示在这里",
      "refresh": "重新读取",
      "binary": "二进制文件，无法按行对比",
      "tooLarge": "文件太大，无法在面板内对比",
      "truncated": "差异过长，仅显示前 {count} 行",
      "noChanges": "该文件在此提交中没有内容变化"
    }
  },
  "en": {
    "gitDiff": {
      "empty": "Click a file inside a commit's file list in the Git Graph to see its diff here",
      "refresh": "Reload",
      "binary": "Binary file — no line-by-line comparison",
      "tooLarge": "The file is too large to diff in this panel",
      "truncated": "The diff is long: only the first {count} rows are shown",
      "noChanges": "This commit does not change the file's contents"
    }
  },
  "ja": {
    "gitDiff": {
      "empty": "Git Graph でコミットのファイルをクリックすると、差分がここに表示されます",
      "refresh": "再読み込み",
      "binary": "バイナリ ファイルのため行単位の比較はできません",
      "tooLarge": "ファイルが大きすぎるためパネル内で比較できません",
      "truncated": "差分が長いため、先頭 {count} 行のみ表示します",
      "noChanges": "このコミットでファイル内容の変更はありません"
    }
  },
  "ko": {
    "gitDiff": {
      "empty": "Git Graph에서 커밋의 파일을 클릭하면 변경 내용이 여기에 표시됩니다",
      "refresh": "다시 읽기",
      "binary": "바이너리 파일이라 줄 단위 비교를 할 수 없습니다",
      "tooLarge": "파일이 너무 커서 패널에서 비교할 수 없습니다",
      "truncated": "변경이 길어 처음 {count}줄만 표시합니다",
      "noChanges": "이 커밋은 파일 내용을 변경하지 않습니다"
    }
  }
}
</i18n>
