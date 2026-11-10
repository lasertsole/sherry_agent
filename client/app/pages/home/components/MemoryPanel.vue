<template>
  <!-- Right-sidebar tab body: mounted and unmounted with its tab (the sidebar owns the
       tab label and the close button), so the mount drives the load. -->
  <div class="flex flex-col gap-3 h-full min-h-0 p-4 min-w-[280px]">
    <!-- Loading skeleton: target tabs + capacity bar + entry cards, matching
         the loaded layout instead of a lone spinner. -->
    <div
      v-if="loading && !entries.length"
      class="flex flex-col gap-3"
      data-test="memory-skeleton">
      <div class="flex gap-1">
        <Skeleton
          v-for="n in 3"
          :key="n"
          width="4rem"
          height="1.8rem" />
      </div>
      <Skeleton
        width="100%"
        height="0.4rem"
        border-radius="9999px" />
      <Skeleton
        v-for="n in 4"
        :key="n"
        height="3.5rem"
        border-radius="0.5rem" />
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

      <!-- Memory settings, moved here from the system-config panel so managing
           entries and configuring their caps live on one surface. Collapsed by
           default: it must not push the entry list away. -->
      <div
        class="shrink-0"
        data-test="memory-settings">
        <button
          type="button"
          class="flex w-full items-center gap-1.5 py-1 text-xs text-gray-500 transition-colors hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200"
          :aria-expanded="showSettings"
          data-test="memory-settings-toggle"
          @click="showSettings = !showSettings">
          <i
            class="pi text-[10px]"
            :class="showSettings ? 'pi-chevron-down' : 'pi-chevron-right'"
            aria-hidden="true" />
          {{ t('memoryPanel.settings') }}
        </button>

        <div
          v-if="showSettings"
          class="flex flex-col gap-3 rounded-lg border border-gray-100 p-3 dark:border-gray-800">
          <p class="m-0 text-xs text-gray-500 dark:text-gray-400">{{ t('memorySettings.hint') }}</p>
          <div
            v-if="settingsError"
            class="text-xs text-red-600 dark:text-red-400"
            data-test="memory-settings-error">
            {{ settingsError }}
          </div>

          <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div class="flex flex-col gap-1">
              <label class="text-xs font-medium text-gray-700 dark:text-gray-200">
                {{ t('memorySettings.memoryLimit') }}
              </label>
              <InputNumber
                v-model="settingsForm.memory_char_limit"
                :min="1"
                :max="8000"
                :use-grouping="false"
                class="w-full" />
            </div>
            <div class="flex flex-col gap-1">
              <label class="text-xs font-medium text-gray-700 dark:text-gray-200">
                {{ t('memorySettings.userLimit') }}
              </label>
              <InputNumber
                v-model="settingsForm.user_char_limit"
                :min="1"
                :max="8000"
                :use-grouping="false"
                class="w-full" />
            </div>
            <div class="flex flex-col gap-1">
              <label class="text-xs font-medium text-gray-700 dark:text-gray-200">
                {{ t('memorySettings.factsLimit') }}
              </label>
              <InputNumber
                v-model="settingsForm.facts_char_limit"
                :min="1"
                :max="8000"
                :use-grouping="false"
                class="w-full" />
            </div>
            <div class="flex flex-col gap-1">
              <label class="text-xs font-medium text-gray-700 dark:text-gray-200">
                {{ t('memorySettings.maxUnusedDays') }}
              </label>
              <InputNumber
                v-model="settingsForm.max_unused_days"
                :min="1"
                :max="3650"
                :use-grouping="false"
                class="w-full" />
              <span class="text-[11px] text-gray-400">{{ t('memorySettings.maxUnusedDaysHint') }}</span>
            </div>
          </div>

          <div class="flex flex-col gap-2 rounded-lg border border-dashed border-gray-200 p-2 dark:border-gray-700">
            <div class="flex items-center gap-2">
              <ToggleSwitch
                v-model="settingsForm.semantic_recall_enabled"
                data-test="memory-semantic-toggle" />
              <span class="text-sm font-medium text-gray-700 dark:text-gray-200">
                {{ t('memorySettings.semanticRecall') }}
              </span>
            </div>
            <div class="grid grid-cols-1 gap-2 sm:grid-cols-2">
              <div class="flex flex-col gap-1">
                <label class="text-xs text-gray-600 dark:text-gray-300">{{ t('memorySettings.semanticTopK') }}</label>
                <InputNumber
                  v-model="settingsForm.semantic_recall_top_k"
                  :min="1"
                  :max="20"
                  :use-grouping="false"
                  :disabled="!settingsForm.semantic_recall_enabled"
                  data-test="memory-semantic-top-k"
                  class="w-full" />
              </div>
              <div class="flex flex-col gap-1">
                <label class="text-xs text-gray-600 dark:text-gray-300">{{ t('memorySettings.semanticBudget') }}</label>
                <InputNumber
                  v-model="settingsForm.semantic_recall_char_budget"
                  :min="200"
                  :max="20000"
                  :use-grouping="false"
                  :disabled="!settingsForm.semantic_recall_enabled"
                  data-test="memory-semantic-budget"
                  class="w-full" />
              </div>
            </div>
            <span class="text-[11px] text-gray-400">{{ t('memorySettings.semanticRecallHint') }}</span>
          </div>

          <div class="flex flex-wrap items-center gap-2">
            <Button
              :label="t('memorySettings.save')"
              icon="pi pi-check"
              size="small"
              :loading="savingSettings"
              :disabled="!settingsDirty"
              data-test="memory-settings-save"
              v-debounce:click.500="handleSaveSettings" />
            <Button
              :label="t('memorySettings.consolidate')"
              icon="pi pi-filter-slash"
              size="small"
              severity="secondary"
              :loading="consolidating"
              data-test="memory-consolidate"
              v-debounce:click.500="handleConsolidate" />
            <span
              v-if="consolidationMessage"
              class="text-xs text-gray-500 dark:text-gray-400">
              {{ consolidationMessage }}
            </span>
          </div>
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
                v-debounce:click.500="() => saveEdit(entry)" />
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
                :loading="busy"
                v-debounce:click.500="() => handleArchive(entry)" />
              <Button
                v-else
                :label="t('memoryPanel.restore')"
                size="small"
                text
                icon="pi pi-replay"
                :loading="busy"
                v-debounce:click.500="() => handleRestore(entry)" />
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
                v-debounce:click.500="handleAdd" />
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
import { useRoute } from 'vue-router';
import { logUtil } from '~/utils/log';
import type { MemoryArchiveReason, MemoryEntry, MemorySettings, MemoryStats, MemoryTarget } from '~/composables/bridge';

const { t } = useI18n({ useScope: 'local' });
const emits = defineEmits<{ saved: [] }>();

/**
 * The session the manual consolidation pass runs under (model + prompt). The
 * panel is a right-sidebar tab of the session page, so the route's sid is the
 * same source the former system-config section used.
 */
const route = useRoute() as unknown as { params?: Record<string, unknown> } | undefined;
const sessionId = computed(() => String(route?.params?.sid ?? ''));

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

// ── Memory settings (moved here from the system-config panel) ──────────────
// The caps / semantic-recall switch / aging window are stored in the memory DB
// and apply live. They keep their own form + dirty state so editing them never
// touches the entry list, and the shared `stats` readout below follows a save.
const DEFAULT_SETTINGS: Pick<
  MemorySettings,
  | 'memory_char_limit'
  | 'user_char_limit'
  | 'facts_char_limit'
  | 'max_unused_days'
  | 'semantic_recall_enabled'
  | 'semantic_recall_top_k'
  | 'semantic_recall_char_budget'
> = {
  memory_char_limit: 2200,
  user_char_limit: 1375,
  facts_char_limit: 1375,
  max_unused_days: 90,
  semantic_recall_enabled: false,
  semantic_recall_top_k: 5,
  semantic_recall_char_budget: 4000
};

/** Collapsed by default: the configuration must not push the entries away. */
const showSettings = ref(false);
const settingsForm = ref({ ...DEFAULT_SETTINGS });
const settingsOriginal = ref({ ...DEFAULT_SETTINGS });
const savingSettings = ref(false);
const consolidating = ref(false);
const consolidationMessage = ref('');
const settingsError = ref('');

const settingsDirty = computed(() =>
  (Object.keys(DEFAULT_SETTINGS) as (keyof typeof DEFAULT_SETTINGS)[]).some(
    key => settingsForm.value[key] !== settingsOriginal.value[key]
  )
);

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

/**
 * Copy a backend settings payload into the editable form + its clean baseline.
 * @param settings The values read from / saved to the backend.
 */
function applySettings(settings: MemorySettings): void {
  settingsForm.value = {
    memory_char_limit: settings.memory_char_limit,
    user_char_limit: settings.user_char_limit,
    facts_char_limit: settings.facts_char_limit,
    max_unused_days: settings.max_unused_days,
    semantic_recall_enabled: settings.semantic_recall_enabled,
    semantic_recall_top_k: settings.semantic_recall_top_k,
    semantic_recall_char_budget: settings.semantic_recall_char_budget
  };
  settingsOriginal.value = { ...settingsForm.value };
}

async function loadSettings(): Promise<void> {
  settingsError.value = '';
  try {
    const settings = await readMemorySettings();
    // A transport stub may answer nothing; keep the defaults instead of
    // rendering NaN fields.
    if (!settings) return;
    applySettings(settings);
  } catch (e) {
    settingsError.value = e instanceof Error ? e.message : String(e);
    logUtil.e('[MemoryPanel] Failed to load memory settings:', e);
  }
}

/** Persist the caps and refresh the capacity readout they define. */
async function handleSaveSettings(): Promise<void> {
  if (savingSettings.value || !settingsDirty.value) return;
  savingSettings.value = true;
  settingsError.value = '';
  try {
    const saved = await writeMemorySettings({ ...settingsForm.value });
    if (saved) applySettings(saved);
    else settingsOriginal.value = { ...settingsForm.value };
    // The capacity bar reads the caps, so a saved limit must re-render at once.
    await load();
    emits('saved');
  } catch (e) {
    settingsError.value = e instanceof Error ? e.message : String(e);
  } finally {
    savingSettings.value = false;
  }
}

/** Run the async aging + LLM consolidation pass under the current session. */
async function handleConsolidate(): Promise<void> {
  if (consolidating.value) return;
  if (!sessionId.value) {
    settingsError.value = t('memorySettings.sessionRequired');
    return;
  }
  consolidating.value = true;
  consolidationMessage.value = '';
  settingsError.value = '';
  try {
    const started = await consolidateMemory(sessionId.value);
    let task = started;
    const deadline = Date.now() + 300_000;
    while (task.status === 'running' && Date.now() < deadline) {
      await new Promise(resolve => setTimeout(resolve, 1000));
      task = await readConsolidationStatus(task.task_id);
    }
    if (task.status === 'running') {
      throw new Error(t('memorySettings.consolidationTimeout'));
    }
    if (task.status === 'failed') {
      throw new Error(task.error || t('memorySettings.consolidationFailed'));
    }
    const archived = task.result?.archived ?? 0;
    const remaining = task.result?.entries_after;
    consolidationMessage.value =
      task.status === 'skipped'
        ? t('memorySettings.consolidationSkipped', { n: archived })
        : t('memorySettings.consolidationDone', { n: archived, m: remaining ?? '?' });
    await load();
    emits('saved');
  } catch (e) {
    settingsError.value = e instanceof Error ? e.message : String(e);
  } finally {
    consolidating.value = false;
  }
}

// The tab's lifetime drives the load (entries + configuration).
onMounted(() => {
  void load();
  void loadSettings();
});

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
      "cancel": "取消",
      "settings": "记忆设置"
    },
    "memorySettings": {
      "hint": "记忆条目保存在 SQLite 中；这里的容量上限与老化窗口立即生效。",
      "memoryLimit": "记忆容量上限（字符）",
      "userLimit": "用户画像容量上限（字符）",
      "factsLimit": "系统事实容量上限（字符）",
      "maxUnusedDays": "未使用老化天数",
      "maxUnusedDaysHint": "超过该天数未使用的条目会在整理时归档（不会删除）。",
      "save": "保存记忆设置",
      "consolidate": "立即整理",
      "consolidationDone": "已整理：归档 {n} 条，剩余 {m} 条",
      "consolidationSkipped": "条目不足，仅执行老化（归档 {n} 条）",
      "consolidationFailed": "整理失败",
      "consolidationTimeout": "整理超时，请稍后重试",
      "sessionRequired": "请先打开一个会话再整理记忆",
      "semanticRecall": "语义召回",
      "semanticRecallHint": "开启后系统提示词只放记忆索引，每轮按需检索相关条目；需要 embedding 与 reranker 模型。",
      "semanticTopK": "召回条数",
      "semanticBudget": "召回字符预算"
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
      "cancel": "Cancel",
      "settings": "Memory settings"
    },
    "memorySettings": {
      "hint": "Memory entries live in SQLite; these caps and the aging window apply immediately.",
      "memoryLimit": "Memory cap (characters)",
      "userLimit": "User-profile cap (characters)",
      "factsLimit": "System-facts cap (characters)",
      "maxUnusedDays": "Unused aging window (days)",
      "maxUnusedDaysHint": "Entries unused for longer than this are archived during consolidation (never deleted).",
      "save": "Save memory settings",
      "consolidate": "Consolidate now",
      "consolidationDone": "Consolidated: {n} archived, {m} remaining",
      "consolidationSkipped": "Too few entries; aging only ({n} archived)",
      "consolidationFailed": "Consolidation failed",
      "consolidationTimeout": "Consolidation timed out; try again later",
      "sessionRequired": "Open a session before consolidating memory",
      "semanticRecall": "Semantic recall",
      "semanticRecallHint": "When on, the system prompt carries only a memory index and relevant entries are recalled per turn; requires the embedding and reranker models.",
      "semanticTopK": "Recall count",
      "semanticBudget": "Recall character budget"
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
      "cancel": "キャンセル",
      "settings": "記憶設定"
    },
    "memorySettings": {
      "hint": "記憶エントリは SQLite に保存されます。この上限と老化期間はすぐに反映されます。",
      "memoryLimit": "記憶の上限（文字数）",
      "userLimit": "ユーザープロファイルの上限（文字数）",
      "factsLimit": "システム事実の上限（文字数）",
      "maxUnusedDays": "未使用の老化日数",
      "maxUnusedDaysHint": "この日数を超えて未使用のエントリは整理時にアーカイブされます（削除されません）。",
      "save": "記憶設定を保存",
      "consolidate": "今すぐ整理",
      "consolidationDone": "整理完了：{n} 件をアーカイブ、残り {m} 件",
      "consolidationSkipped": "件数が少ないため老化のみ実行（{n} 件をアーカイブ）",
      "consolidationFailed": "整理に失敗しました",
      "consolidationTimeout": "整理がタイムアウトしました。後でもう一度お試しください",
      "sessionRequired": "記憶を整理する前にセッションを開いてください",
      "semanticRecall": "意味検索",
      "semanticRecallHint": "オンにするとシステムプロンプトは記憶インデックスのみになり、ターンごとに関連エントリを検索します。embedding と reranker モデルが必要です。",
      "semanticTopK": "検索件数",
      "semanticBudget": "検索の文字予算"
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
      "cancel": "취소",
      "settings": "기억 설정"
    },
    "memorySettings": {
      "hint": "기억 항목은 SQLite에 저장됩니다. 여기의 상한과 노화 기간은 즉시 적용됩니다.",
      "memoryLimit": "기억 용량 상한(문자)",
      "userLimit": "사용자 프로필 상한(문자)",
      "factsLimit": "시스템 사실 상한(문자)",
      "maxUnusedDays": "미사용 노화 일수",
      "maxUnusedDaysHint": "이 일수보다 오래 사용되지 않은 항목은 정리 시 보관됩니다(삭제되지 않음).",
      "save": "기억 설정 저장",
      "consolidate": "지금 정리",
      "consolidationDone": "정리 완료: {n}개 보관, {m}개 남음",
      "consolidationSkipped": "항목이 적어 노화만 실행({n}개 보관)",
      "consolidationFailed": "정리에 실패했습니다",
      "consolidationTimeout": "정리 시간이 초과되었습니다. 나중에 다시 시도하세요",
      "sessionRequired": "기억을 정리하기 전에 세션을 여세요",
      "semanticRecall": "의미 검색",
      "semanticRecallHint": "켜면 시스템 프롬프트에는 기억 색인만 들어가고 매 턴 관련 항목을 검색합니다. embedding과 reranker 모델이 필요합니다.",
      "semanticTopK": "검색 개수",
      "semanticBudget": "검색 문자 예산"
    }
  }
}
</i18n>
