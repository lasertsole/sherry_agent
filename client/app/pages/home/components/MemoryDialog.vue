<template>
  <Dialog
    v-model:visible="visible"
    :header="t('config.memory.title')"
    :modal="true"
    :closable="true"
    class="w-[95vw] md:w-[1100px]"
    @show="loadContent">
    <div class="flex flex-col gap-3">
      <div
        v-if="loading"
        class="flex items-center justify-center py-8">
        <ProgressSpinner style="width: 2rem; height: 2rem" />
      </div>
      <template v-else>
        <TabView v-model:activeIndex="activeTab">
          <TabPanel
            v-for="tab in tabs"
            :key="tab.file"
            :value="tab.file"
            :header="t(tab.i18nKey)">
            <div class="flex flex-col gap-2">
              <div class="flex items-center justify-between">
                <span class="text-sm text-gray-500 dark:text-gray-400">{{ t(tab.i18nDescKey) }}</span>
                <span
                  :class="[
                    'text-xs',
                    joinedLength(tab.file) > MAX_CHARS
                      ? 'text-red-500'
                      : joinedLength(tab.file) > MAX_CHARS * 0.9
                        ? 'text-orange-500'
                        : 'text-gray-400'
                  ]">
                  {{ joinedLength(tab.file) }} / {{ MAX_CHARS }}
                </span>
              </div>

              <div class="text-xs text-yellow-500 dark:text-yellow-400">
                {{ t('config.memory.effectiveHint') }}
              </div>

              <div
                v-if="!entries(tab.file).length"
                class="text-sm text-gray-400 dark:text-gray-500 pb-2">
                {{ t('config.memory.empty') }}
              </div>

              <div
                v-for="(entry, idx) in entries(tab.file)"
                :key="idx"
                class="flex flex-col gap-1 rounded-lg border border-gray-200 dark:border-gray-700 p-2">
                <div class="flex items-center justify-between">
                  <span class="text-xs text-gray-400 dark:text-gray-500">
                    #{{ idx + 1 }} · {{ entryLength(tab.file, idx) }} chars
                  </span>
                  <Button
                    icon="pi pi-trash"
                    text
                    severity="danger"
                    :aria-label="t('config.memory.deleteEntry')"
                    class="!w-8 !h-8 !p-0"
                    @click="removeEntry(tab.file, idx)" />
                </div>
                <Textarea
                  :model-value="entries(tab.file)[idx]"
                  rows="3"
                  class="w-full font-mono text-sm"
                  autoResize
                  style="min-height: 4.5rem; max-height: 24rem"
                  @update:model-value="setEntry(tab.file, idx, $event)" />
              </div>

              <Button
                :label="t('config.memory.addEntry')"
                icon="pi pi-plus"
                text
                severity="secondary"
                class="self-start"
                @click="addEntry(tab.file)" />
            </div>
          </TabPanel>
        </TabView>
      </template>
    </div>
    <template #footer>
      <div class="flex gap-2 justify-end">
        <Button
          :label="t('config.cancel')"
          icon="pi pi-times"
          severity="secondary"
          @click="visible = false" />
        <Button
          :label="t('config.save')"
          icon="pi pi-check"
          :loading="saving"
          :disabled="!canSave"
          @click="handleSave" />
      </div>
    </template>
  </Dialog>
</template>

<script lang="ts" setup>
import { ref, computed } from 'vue';
import { useI18n } from 'vue-i18n';
import { readMemory, writeMemory } from '@/composables/bridge';

/**
 * Memory files are stored on disk as a header line + a list of entries
 * delimited by "\n§\n" (see agent/tools/memory.py ENTRY_DELIMITER). Each
 * entry may itself contain multiple lines. The raw file's first line is an
 * H1 title ("# MEMORY" / "# USER") that acts as a file header — it is NOT a
 * memory entry. The backend (server/service/memory.py) returns the whole
 * file verbatim; the frontend must split entries while treating the header
 * as metadata, and re-attach it when persisting.
 */
const ENTRY_DELIMITER = '\n§\n';

/**
 * Derive the H1 title used as a file header (e.g. 'MEMORY.md' -> '# MEMORY').
 * Only the exact title of the current file is treated as a header.
 */
function fileTitle(file: string): string {
  return `# ${file.replace(/\.md$/i, '')}`;
}

/**
 * Re-assemble a full raw file body for a given file: header line, then the
 * entries joined by the on-disk delimiter. Mirrors the on-disk layout the
 * backend writes and expects to read back.
 */
function joinFileBody(file: string, entries: string[]): string {
  const body = entries.filter(e => e.trim().length > 0).join(ENTRY_DELIMITER);
  if (!body) return fileTitle(file);
  return `${fileTitle(file)}${ENTRY_DELIMITER}${body}`;
}

const { t } = useI18n({ useScope: 'local' });

const props = defineProps<{ modelValue: boolean }>();
const emits = defineEmits<{ 'update:modelValue': [value: boolean]; saved: [] }>();

const visible = computed({
  get: () => props.modelValue,
  set: v => emits('update:modelValue', v)
});

// Whole-file character budget. Mirrors server/service/memory.py write_memory_files,
// which rejects len(content) > 8_000 on the joined raw file.
const MAX_CHARS = 8000;

interface MemoryTab {
  /** Actual filename under workspace/memory/ (e.g. 'MEMORY.md'). */
  file: string;
  i18nKey: string;
  i18nDescKey: string;
}

const tabs: MemoryTab[] = [
  { file: 'MEMORY.md', i18nKey: 'config.tabs.memoryAgent', i18nDescKey: 'config.desc.memoryAgent' },
  { file: 'USER.md', i18nKey: 'config.tabs.memoryUser', i18nDescKey: 'config.desc.memoryUser' }
] as const;

const activeTab = ref(0);
const loading = ref(false);
const saving = ref(false);
/** Live editable entries per file (array copy so v-model indexes stay stable). */
const editEntries = ref<Record<string, string[]>>({});
/** Frozen entries per file at load time, for dirty tracking. */
const originalEntries = ref<Record<string, string[]>>({});

/** Split a raw memory file into entries using the on-disk delimiter. */
function splitEntries(file: string, raw: string): string[] {
  if (!raw || !raw.trim()) return [];

  const header = fileTitle(file);
  const parts = raw
    .split(ENTRY_DELIMITER)
    .map(e => e.trim())
    .filter(e => e.length > 0);

  // The file's first line is an H1 title that matches this file's name
  // ("# MEMORY" / "# USER") and acts as a file header, not a memory entry.
  // Split it off so it never appears as an editable entry in the UI.
  if (parts.length > 0 && parts[0] === header) {
    parts.shift();
  }

  return parts;
}

/** Join entries back into a raw file using the on-disk delimiter. */
function joinEntries(entries: string[]): string {
  return entries.filter(e => e.trim().length > 0).join(ENTRY_DELIMITER);
}

function entries(file: string): string[] {
  return editEntries.value[file] ?? [];
}

/** Length of the joined raw file for one tab (what the server actually measures). */
function joinedLength(file: string): number {
  return joinEntries(entries(file)).length;
}

function entryLength(file: string, idx: number): number {
  return (entries(file)[idx] ?? '').length;
}

function addEntry(file: string) {
  editEntries.value[file]?.push('');
}

function removeEntry(file: string, idx: number) {
  editEntries.value[file]?.splice(idx, 1);
}

function setEntry(file: string, idx: number, value: string) {
  const list = editEntries.value[file];
  if (list) list[idx] = value;
}

const loadContent = async () => {
  loading.value = true;
  try {
    const data = await readMemory();
    const parsed: Record<string, string[]> = {};
    for (const tab of tabs) {
      parsed[tab.file] = splitEntries(tab.file, data[tab.file] ?? '');
    }
    editEntries.value = parsed;
    originalEntries.value = {
      ...Object.fromEntries(Object.entries(parsed).map(([k, v]) => [k, [...v]]))
    };
  } catch (e) {
    console.error('[MemoryDialog] Failed to load content:', e);
  } finally {
    loading.value = false;
  }
};

const canSave = computed(() => {
  if (loading.value || saving.value) return false;
  const tab = tabs[activeTab.value];
  if (!tab) return false;
  const len = joinedLength(tab.file);
  return len > 0 && len <= MAX_CHARS && isDirty(tab.file);
});

function fileChanged(file: string): boolean {
  return joinEntries(entries(file)) !== joinEntries(originalEntries.value[file] ?? []);
}

function isDirty(file: string): boolean {
  const a = editEntries.value[file] ?? [];
  const b = originalEntries.value[file] ?? [];
  if (a.length !== b.length) return true;
  return a.some((e, i) => e !== b[i]);
}

const handleSave = async () => {
  saving.value = true;
  try {
    // Persist only the changed memory files, as full-file content (header + entries).
    const changed: Record<string, string> = {};
    for (const tab of tabs) {
      if (fileChanged(tab.file)) {
        changed[tab.file] = joinFileBody(tab.file, entries(tab.file));
      }
    }
    if (Object.keys(changed).length > 0) {
      await writeMemory(changed);
    }
    emits('saved');
    visible.value = false;
  } catch (e) {
    console.error('[MemoryDialog] Failed to save:', e);
  } finally {
    saving.value = false;
  }
};
</script>

<i18n lang="json">
{
  "zh": {
    "config": {
      "memory": {
        "title": "记忆",
        "addEntry": "添加条目",
        "deleteEntry": "删除条目",
        "empty": "暂无条目，请在下方添加。",
        "effectiveHint": "提示：修改后仅在系统压缩或重启时生效。"
      }
    }
  },
  "en": {
    "config": {
      "memory": {
        "title": "Memory",
        "addEntry": "Add entry",
        "deleteEntry": "Delete entry",
        "empty": "No entries yet. Add one below.",
        "effectiveHint": "Note: Changes only take effect after compression or restart."
      }
    }
  },
  "ja": {
    "config": {
      "memory": {
        "title": "記憶",
        "addEntry": "項目を追加",
        "deleteEntry": "項目を削除",
        "empty": "項目はまだありません。下から追加してください。",
        "effectiveHint": "注意：変更は圧縮時または再起動時にのみ反映されます。"
      }
    }
  },
  "ko": {
    "config": {
      "memory": {
        "title": "메모리",
        "addEntry": "항목 추가",
        "deleteEntry": "항목 삭제",
        "empty": "항목이 없습니다. 아래에서 추가하세요.",
        "effectiveHint": "참고: 변경 사항은 압축 또는 재시작 시에만 적용됩니다."
      }
    }
  }
}
</i18n>
