<template>
  <!-- Right-sidebar tab body: mounted and unmounted with its tab (the sidebar owns the
       tab label and the close button), so the mount drives the load. -->
  <div class="flex flex-col gap-3 h-full min-h-0 p-4">
    <div
      v-if="loading && !entries.length"
      class="flex items-center justify-center py-8">
      <ProgressSpinner style="width: 2rem; height: 2rem" />
    </div>
    <template v-else>
      <!-- Target tabs (记忆 / 用户画像 / 系统事实). A plain button row keeps the
           list mounted across targets instead of one TabPanel tree per target. -->
      <div class="flex shrink-0 gap-1 border-b border-gray-200 dark:border-gray-700">
        <button
          v-for="tab in TARGETS"
          :key="tab.value"
          type="button"
          class="flex items-center gap-1 border-b-2 px-2 py-1.5 text-sm transition-colors"
          :class="
            activeTarget === tab.value
              ? 'border-theme-main text-theme-main'
              : 'border-transparent text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200'
          "
          @click="switchTarget(tab.value)">
          {{ t(tab.i18nKey) }}
          <span class="text-xs text-gray-400">({{ stats?.targets[tab.value]?.count ?? 0 }})</span>
        </button>
      </div>

      <!-- Capacity readout for the active target. -->
      <div
        v-if="targetStats"
        class="shrink-0 flex flex-col gap-1">
        <div class="flex items-center justify-between text-xs text-gray-500 dark:text-gray-400">
          <span>
            {{ t('memoryPanel.stats.entries', { n: targetStats.count }) }} · {{ targetStats.chars }} /
            {{ targetStats.limit }} chars
          </span>
          <span :class="percentClass(targetStats.percent)">{{ targetStats.percent }}%</span>
        </div>
        <div class="h-1.5 w-full overflow-hidden rounded-full bg-gray-200 dark:bg-gray-700">
          <div
            class="h-full rounded-full bg-theme-main transition-[width] duration-300"
            :style="{ width: `${targetStats.percent}%` }" />
        </div>
        <div class="flex items-center gap-2 text-xs text-gray-400">
          <span>{{ t('memoryPanel.stats.archived', { n: targetStats.archived }) }}</span>
          <span>·</span>
          <span>{{ t('memoryPanel.stats.avgUsage', { n: targetStats.avg_usage.toFixed(1) }) }}</span>
        </div>
      </div>

      <!-- Toolbar: search, sort, archived toggle, archive reason. -->
      <div class="shrink-0 flex flex-wrap items-center gap-2">
        <InputText
          v-model="search"
          :placeholder="t('memoryPanel.searchPlaceholder')"
          class="min-w-0 flex-1 text-sm"
          data-test="memory-search" />
        <Select
          v-model="sort"
          :options="sortOptions"
          option-label="label"
          option-value="value"
          class="w-36 text-sm"
          data-test="memory-sort" />
        <div class="flex items-center gap-1 text-xs text-gray-500 dark:text-gray-400">
          <ToggleSwitch
            v-model="showArchived"
            data-test="memory-show-archived" />
          <span>{{ t('memoryPanel.includeArchived') }}</span>
        </div>
      </div>

      <div class="shrink-0 flex items-center gap-2">
        <span class="text-xs text-gray-500 dark:text-gray-400">{{ t('memoryPanel.archiveReason') }}</span>
        <Select
          v-model="archiveReason"
          :options="reasonOptions"
          option-label="label"
          option-value="value"
          class="w-40 text-sm" />
      </div>

      <div
        v-if="error"
        class="shrink-0 text-xs text-red-600 dark:text-red-400">
        {{ error }}
      </div>

      <!-- Entry list. -->
      <div class="min-h-0 flex-1 overflow-y-auto flex flex-col gap-2 pr-0.5">
        <div
          v-if="!visibleEntries.length"
          class="py-3 text-sm text-gray-400 dark:text-gray-500">
          {{ t(search ? 'memoryPanel.noMatches' : 'memoryPanel.empty') }}
        </div>

        <div
          v-for="entry in visibleEntries"
          :key="entry.id"
          class="flex flex-col gap-1 rounded-lg border border-gray-200 p-2 dark:border-gray-700"
          :class="entry.archived ? 'opacity-60' : ''"
          :data-test="`memory-entry-${entry.id}`">
          <div class="flex flex-wrap items-center gap-1.5">
            <Tag
              v-if="entry.entry_type"
              :value="typeLabel(entry)"
              severity="secondary"
              class="text-[10px]" />
            <Tag
              v-if="entry.archived"
              :value="reasonLabel(entry)"
              severity="warning"
              class="text-[10px]" />
            <span class="text-[11px] text-gray-400">
              #{{ entry.id }} · {{ t('memoryPanel.entry.usage', { n: entry.usage_count }) }}
            </span>
            <span class="text-[11px] text-gray-400">
              · {{ t('memoryPanel.entry.created', { date: formatDate(entry.created_at) }) }}
            </span>
            <span
              v-if="entry.last_used"
              class="text-[11px] text-gray-400">
              · {{ t('memoryPanel.entry.lastUsed', { date: formatDate(entry.last_used) }) }}
            </span>
          </div>
          <div
            v-if="entry.origin_session"
            class="truncate text-[10px] text-gray-400"
            :title="entry.origin_session">
            {{ t('memoryPanel.entry.origin', { sid: entry.origin_session }) }}
          </div>

          <template v-if="editingId === entry.id">
            <Textarea
              v-model="editText"
              rows="3"
              class="w-full font-mono text-sm"
              autoResize
              style="min-height: 4.5rem; max-height: 24rem" />
            <div class="flex justify-end gap-1">
              <Button
                :label="t('memoryPanel.cancel')"
                size="small"
                text
                severity="secondary"
                @click="cancelEdit" />
              <Button
                :label="t('memoryPanel.save')"
                size="small"
                icon="pi pi-check"
                :loading="busy"
                :disabled="!editText.trim()"
                @click="saveEdit(entry)" />
            </div>
          </template>
          <template v-else>
            <p class="m-0 whitespace-pre-wrap break-words text-sm">{{ entry.content }}</p>
            <div class="flex justify-end gap-1">
              <Button
                v-if="!entry.archived"
                :label="t('memoryPanel.edit')"
                size="small"
                text
                icon="pi pi-pencil"
                @click="startEdit(entry)" />
              <Button
                v-if="!entry.archived"
                :label="t('memoryPanel.archive')"
                size="small"
                text
                severity="danger"
                icon="pi pi-inbox"
                @click="handleArchive(entry)" />
              <Button
                v-else
                :label="t('memoryPanel.restore')"
                size="small"
                text
                icon="pi pi-replay"
                @click="handleRestore(entry)" />
            </div>
          </template>
        </div>

        <Button
          v-if="entries.length < total"
          :label="t('memoryPanel.loadMore')"
          size="small"
          text
          class="self-center"
          :loading="loading"
          @click="loadMore" />
      </div>

      <!-- Add form. -->
      <div class="shrink-0 flex flex-col gap-2 border-t border-gray-200 pt-2 dark:border-gray-700">
        <template v-if="adding">
          <Textarea
            v-model="newText"
            :placeholder="t('memoryPanel.addPlaceholder')"
            rows="3"
            class="w-full font-mono text-sm"
            autoResize
            style="min-height: 4.5rem; max-height: 24rem"
            data-test="memory-new-content" />
          <div class="flex items-center gap-2">
            <Select
              v-model="newType"
              :options="typeOptions"
              option-label="label"
              option-value="value"
              :placeholder="t('memoryPanel.entryType')"
              class="w-40 text-sm"
              show-clear />
            <div class="flex flex-1 justify-end gap-1">
              <Button
                :label="t('memoryPanel.cancel')"
                size="small"
                text
                severity="secondary"
                @click="cancelAdd" />
              <Button
                :label="t('memoryPanel.save')"
                size="small"
                icon="pi pi-check"
                :loading="busy"
                :disabled="!newText.trim()"
                @click="handleAdd" />
            </div>
          </div>
        </template>
        <Button
          v-else
          :label="t('memoryPanel.addEntry')"
          icon="pi pi-plus"
          size="small"
          text
          severity="secondary"
          class="self-start"
          data-test="memory-add"
          @click="adding = true" />
      </div>
    </template>
  </div>
</template>

<script lang="ts" setup>
import { computed, onMounted, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import { logUtil } from '~/utils/log';
import type { MemoryArchiveReason, MemoryEntry, MemoryStats, MemoryTarget } from '~/composables/bridge';

const { t } = useI18n({ useScope: 'local' });
const emits = defineEmits<{ saved: [] }>();

const TARGETS: { value: MemoryTarget; i18nKey: string }[] = [
  { value: 'memory', i18nKey: 'memoryPanel.tabs.memory' },
  { value: 'user', i18nKey: 'memoryPanel.tabs.user' },
  { value: 'facts', i18nKey: 'memoryPanel.tabs.facts' }
];

const PAGE_SIZE = 100;
const activeTarget = ref<MemoryTarget>('memory');
const entries = ref<MemoryEntry[]>([]);
const total = ref(0);
const stats = ref<MemoryStats | null>(null);
const loading = ref(false);
const busy = ref(false);
const error = ref('');
const search = ref('');
const sort = ref<'created' | 'usage' | 'last_used'>('created');
const showArchived = ref(false);
const archiveReason = ref<MemoryArchiveReason>('manual');
const editingId = ref<number | null>(null);
const editText = ref('');
const adding = ref(false);
const newText = ref('');
const newType = ref<string | null>(null);

const sortOptions = computed(() => [
  { label: t('memoryPanel.sorts.created'), value: 'created' },
  { label: t('memoryPanel.sorts.usage'), value: 'usage' },
  { label: t('memoryPanel.sorts.lastUsed'), value: 'last_used' }
]);
const reasonOptions = computed(() =>
  (['manual', 'evicted', 'contradicted', 'consolidated', 'superseded'] as MemoryArchiveReason[]).map(value => ({
    label: t(`memoryPanel.reasons.${value}`),
    value
  }))
);
const typeOptions = computed(() =>
  ['project', 'user', 'feedback', 'reference', 'system'].map(value => ({
    label: t(`memoryPanel.types.${value}`),
    value
  }))
);
const targetStats = computed(() => stats.value?.targets[activeTarget.value] ?? null);
const visibleEntries = computed(() => {
  const needle = search.value.trim().toLowerCase();
  if (!needle) return entries.value;
  return entries.value.filter(entry => entry.content.toLowerCase().includes(needle));
});

function formatDate(ms: number | null): string {
  if (!ms) return '—';
  try {
    return new Date(ms).toLocaleDateString();
  } catch {
    return '—';
  }
}

function percentClass(percent: number): string {
  if (percent >= 90) return 'text-red-500';
  if (percent >= 70) return 'text-orange-500';
  return 'text-gray-400';
}

async function load(reset = true) {
  loading.value = true;
  error.value = '';
  try {
    const offset = reset ? 0 : entries.value.length;
    const page = await readMemoryEntries({
      target: activeTarget.value,
      includeArchived: showArchived.value,
      sort: sort.value,
      order: 'desc',
      limit: PAGE_SIZE,
      offset
    });
    entries.value = reset ? page.entries : [...entries.value, ...page.entries];
    total.value = page.total;
    stats.value = await readMemoryStats();
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
    logUtil.e('[MemoryPanel] Failed to load entries:', e);
  } finally {
    loading.value = false;
  }
}

function loadMore() {
  void load(false);
}

function switchTarget(target: MemoryTarget) {
  if (activeTarget.value === target) return;
  activeTarget.value = target;
  editingId.value = null;
  search.value = '';
  void load();
}

function startEdit(entry: MemoryEntry) {
  editingId.value = entry.id;
  editText.value = entry.content;
}

function cancelEdit() {
  editingId.value = null;
  editText.value = '';
}

async function saveEdit(entry: MemoryEntry) {
  busy.value = true;
  error.value = '';
  try {
    await updateMemoryEntry(entry.id, { content: editText.value });
    cancelEdit();
    await load();
    emits('saved');
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
  } finally {
    busy.value = false;
  }
}

async function handleArchive(entry: MemoryEntry) {
  busy.value = true;
  error.value = '';
  try {
    await archiveMemoryEntry(entry.id, archiveReason.value);
    if (editingId.value === entry.id) cancelEdit();
    await load();
    emits('saved');
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
  } finally {
    busy.value = false;
  }
}

async function handleRestore(entry: MemoryEntry) {
  busy.value = true;
  error.value = '';
  try {
    await restoreMemoryEntry(entry.id);
    await load();
    emits('saved');
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
  } finally {
    busy.value = false;
  }
}

async function handleAdd() {
  busy.value = true;
  error.value = '';
  try {
    await createMemoryEntry({
      target: activeTarget.value,
      content: newText.value,
      entry_type: newType.value
    });
    newText.value = '';
    newType.value = null;
    adding.value = false;
    await load();
    emits('saved');
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
  } finally {
    busy.value = false;
  }
}

function cancelAdd() {
  adding.value = false;
  newText.value = '';
  newType.value = null;
}

function typeLabel(entry: MemoryEntry): string {
  if (!entry.entry_type) return '';
  return t(`memoryPanel.types.${entry.entry_type}`);
}

function reasonLabel(entry: MemoryEntry): string {
  return t(`memoryPanel.reasons.${entry.archived_reason ?? 'manual'}`);
}

// The tab's lifetime drives the load.
onMounted(() => void load());

// The two toolbar controls change the query, not just the view.
watch([sort, showArchived], () => void load());
</script>

<i18n lang="json">
{
  "zh": {
    "memoryPanel": {
      "tabs": { "memory": "记忆", "user": "用户画像", "facts": "系统事实" },
      "searchPlaceholder": "搜索条目…",
      "sorts": { "created": "按创建时间", "usage": "按使用次数", "lastUsed": "按最后使用" },
      "includeArchived": "含已归档",
      "archiveReason": "归档原因",
      "reasons": {
        "manual": "手动",
        "evicted": "老化淘汰",
        "contradicted": "被推翻",
        "consolidated": "已合并",
        "superseded": "被替代"
      },
      "types": {
        "project": "项目",
        "user": "用户",
        "feedback": "反馈",
        "reference": "参考",
        "system": "系统"
      },
      "stats": {
        "entries": "{n} 条",
        "archived": "归档 {n}",
        "avgUsage": "平均使用 {n} 次"
      },
      "entry": {
        "usage": "使用 {n} 次",
        "created": "创建 {date}",
        "lastUsed": "最近使用 {date}",
        "origin": "来源会话：{sid}"
      },
      "empty": "暂无条目",
      "noMatches": "没有匹配的条目",
      "loadMore": "加载更多",
      "addEntry": "新增条目",
      "addPlaceholder": "输入要保存的记忆内容…",
      "entryType": "条目类型",
      "edit": "编辑",
      "archive": "归档",
      "restore": "恢复",
      "save": "保存",
      "cancel": "取消"
    }
  },
  "en": {
    "memoryPanel": {
      "tabs": { "memory": "Memory", "user": "User profile", "facts": "System facts" },
      "searchPlaceholder": "Search entries…",
      "sorts": { "created": "By created", "usage": "By usage", "lastUsed": "By last used" },
      "includeArchived": "Include archived",
      "archiveReason": "Archive reason",
      "reasons": {
        "manual": "Manual",
        "evicted": "Evicted",
        "contradicted": "Contradicted",
        "consolidated": "Consolidated",
        "superseded": "Superseded"
      },
      "types": {
        "project": "Project",
        "user": "User",
        "feedback": "Feedback",
        "reference": "Reference",
        "system": "System"
      },
      "stats": {
        "entries": "{n} entries",
        "archived": "{n} archived",
        "avgUsage": "{n} uses avg."
      },
      "entry": {
        "usage": "used {n}×",
        "created": "created {date}",
        "lastUsed": "last used {date}",
        "origin": "Origin session: {sid}"
      },
      "empty": "No entries yet",
      "noMatches": "No matching entries",
      "loadMore": "Load more",
      "addEntry": "Add entry",
      "addPlaceholder": "What should be remembered?",
      "entryType": "Entry type",
      "edit": "Edit",
      "archive": "Archive",
      "restore": "Restore",
      "save": "Save",
      "cancel": "Cancel"
    }
  },
  "ja": {
    "memoryPanel": {
      "tabs": { "memory": "記憶", "user": "ユーザープロファイル", "facts": "システム事実" },
      "searchPlaceholder": "エントリを検索…",
      "sorts": { "created": "作成日順", "usage": "使用回数順", "lastUsed": "最終使用順" },
      "includeArchived": "アーカイブを含む",
      "archiveReason": "アーカイブ理由",
      "reasons": {
        "manual": "手動",
        "evicted": "寿命による削除",
        "contradicted": "矛盾",
        "consolidated": "統合済み",
        "superseded": "置き換え"
      },
      "types": {
        "project": "プロジェクト",
        "user": "ユーザー",
        "feedback": "フィードバック",
        "reference": "参考",
        "system": "システム"
      },
      "stats": {
        "entries": "{n} 件",
        "archived": "アーカイブ {n} 件",
        "avgUsage": "平均使用 {n} 回"
      },
      "entry": {
        "usage": "使用 {n} 回",
        "created": "作成 {date}",
        "lastUsed": "最終使用 {date}",
        "origin": "作成セッション：{sid}"
      },
      "empty": "エントリはありません",
      "noMatches": "一致するエントリはありません",
      "loadMore": "さらに読み込む",
      "addEntry": "エントリを追加",
      "addPlaceholder": "記憶する内容を入力…",
      "entryType": "エントリ種別",
      "edit": "編集",
      "archive": "アーカイブ",
      "restore": "復元",
      "save": "保存",
      "cancel": "キャンセル"
    }
  },
  "ko": {
    "memoryPanel": {
      "tabs": { "memory": "기억", "user": "사용자 프로필", "facts": "시스템 사실" },
      "searchPlaceholder": "항목 검색…",
      "sorts": { "created": "생성순", "usage": "사용 횟수순", "lastUsed": "최근 사용순" },
      "includeArchived": "보관 포함",
      "archiveReason": "보관 사유",
      "reasons": {
        "manual": "수동",
        "evicted": "노화 제거",
        "contradicted": "모순",
        "consolidated": "통합됨",
        "superseded": "대체됨"
      },
      "types": {
        "project": "프로젝트",
        "user": "사용자",
        "feedback": "피드백",
        "reference": "참고",
        "system": "시스템"
      },
      "stats": {
        "entries": "{n}개",
        "archived": "보관 {n}개",
        "avgUsage": "평균 사용 {n}회"
      },
      "entry": {
        "usage": "사용 {n}회",
        "created": "생성 {date}",
        "lastUsed": "최근 사용 {date}",
        "origin": "생성 세션: {sid}"
      },
      "empty": "항목이 없습니다",
      "noMatches": "일치하는 항목이 없습니다",
      "loadMore": "더 불러오기",
      "addEntry": "항목 추가",
      "addPlaceholder": "기억할 내용을 입력하세요…",
      "entryType": "항목 유형",
      "edit": "편집",
      "archive": "보관",
      "restore": "복원",
      "save": "저장",
      "cancel": "취소"
    }
  }
}
</i18n>
