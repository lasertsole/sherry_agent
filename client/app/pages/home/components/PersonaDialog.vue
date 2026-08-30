<template>
  <Dialog
    v-model:visible="visible"
    :header="t('config.persona.title')"
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
            :key="tab.key"
            :value="tab.key"
            :header="t(tab.i18nKey)">
            <div class="flex flex-col gap-2">
              <div class="flex items-center justify-between">
                <span class="text-sm text-gray-500 dark:text-gray-400">{{ t(tab.i18nDescKey) }}</span>
                <div class="flex items-center gap-2">
                  <Button
                    :label="t('config.persona.restoreDefault')"
                    icon="pi pi-refresh"
                    severity="secondary"
                    text
                    size="small"
                    :loading="restoring"
                    :disabled="restoring"
                    @click="restoreDefault(tab)" />
                  <span
                    :class="[
                      'text-xs',
                      (editContent[tab.key]?.length ?? 0) > MAX_CHARS
                        ? 'text-red-500'
                        : (editContent[tab.key]?.length ?? 0) > MAX_CHARS * 0.9
                          ? 'text-orange-500'
                          : 'text-gray-400'
                    ]">
                    {{ editContent[tab.key]?.length ?? 0 }} / {{ MAX_CHARS }}
                  </span>
                </div>
              </div>
              <Textarea
                v-model="editContent[tab.key]"
                rows="12"
                class="w-full font-mono text-sm"
                :maxlength="MAX_CHARS"
                style="height: 72vh; min-height: 72vh; max-height: 72vh; overflow: auto" />
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
import { readSystemPrompt, writeSystemPrompt, readSystemPromptTemplate } from '@/composables/bridge';

const { t, locale } = useI18n({ useScope: 'local' });

const props = defineProps<{ modelValue: boolean }>();
const emits = defineEmits<{ 'update:modelValue': [value: boolean]; saved: [] }>();

const visible = computed({
  get: () => props.modelValue,
  set: v => emits('update:modelValue', v)
});

const MAX_CHARS = 2000;

interface PersonaTab {
  /** Unique key for the tab; the same basename may exist across layers. */
  key: string;
  /** Actual filename sent to the backend (e.g. 'AGENTS.md'). */
  file: string;
  i18nKey: string;
  i18nDescKey: string;
  /** Read function that returns a file->content map for this tab's group. */
  readFn: () => Promise<Record<string, string>>;
  /** Write function that persists only the given changed files. */
  writeFn: (fileToContent: Record<string, string>) => Promise<void>;
}

const tabs: PersonaTab[] = [
  {
    key: 'AGENTS.md',
    file: 'AGENTS.md',
    i18nKey: 'config.tabs.agents',
    i18nDescKey: 'config.desc.agents',
    readFn: readSystemPrompt,
    writeFn: writeSystemPrompt
  },
  {
    key: 'IDENTITY.md',
    file: 'IDENTITY.md',
    i18nKey: 'config.tabs.identity',
    i18nDescKey: 'config.desc.identity',
    readFn: readSystemPrompt,
    writeFn: writeSystemPrompt
  },
  {
    key: 'SOUL.md',
    file: 'SOUL.md',
    i18nKey: 'config.tabs.soul',
    i18nDescKey: 'config.desc.soul',
    readFn: readSystemPrompt,
    writeFn: writeSystemPrompt
  },
  {
    key: 'USER.md',
    file: 'USER.md',
    i18nKey: 'config.tabs.user',
    i18nDescKey: 'config.desc.user',
    readFn: readSystemPrompt,
    writeFn: writeSystemPrompt
  }
] as const;

const activeTab = ref(0);
const loading = ref(false);
const saving = ref(false);
const restoring = ref(false);
const editContent = ref<Record<string, string>>({});
const originalContent = ref<Record<string, string>>({});

const loadContent = async () => {
  loading.value = true;
  try {
    // Group tabs by their read function so each group is fetched once.
    const byReadFn = new Map<PersonaTab['readFn'], PersonaTab[]>();
    for (const tab of tabs) {
      const list = byReadFn.get(tab.readFn) ?? [];
      list.push(tab);
      byReadFn.set(tab.readFn, list);
    }
    const content: Record<string, string> = {};
    for (const [readFn, groupTabs] of byReadFn) {
      const data = await readFn();
      for (const tab of groupTabs) {
        content[tab.key] = data[tab.file] ?? '';
      }
    }
    editContent.value = { ...content };
    originalContent.value = { ...content };
  } catch (e) {
    console.error('[PersonaDialog] Failed to load content:', e);
  } finally {
    loading.value = false;
  }
};

const canSave = computed(() => {
  if (loading.value || saving.value) return false;
  const tab = tabs[activeTab.value];
  if (!tab) return false;
  const len = editContent.value[tab.key]?.length ?? 0;
  return len > 0 && len <= MAX_CHARS;
});

const restoreDefault = async (tab: PersonaTab) => {
  restoring.value = true;
  try {
    const content = await readSystemPromptTemplate(locale.value);
    editContent.value[tab.key] = content[tab.file] ?? '';
  } catch (e) {
    console.error('[PersonaDialog] Failed to restore default:', e);
  } finally {
    restoring.value = false;
  }
};

const handleSave = async () => {
  saving.value = true;
  try {
    // Collect changed files and persist them. All tabs share the same write
    // function, but grouping keeps the structure reusable (single save call).
    const byWriteFn = new Map<PersonaTab['writeFn'], Record<string, string>>();
    for (const tab of tabs) {
      const content = editContent.value[tab.key] ?? '';
      if (content !== (originalContent.value[tab.key] ?? '')) {
        const map = byWriteFn.get(tab.writeFn) ?? {};
        map[tab.file] = content;
        byWriteFn.set(tab.writeFn, map);
      }
    }
    for (const [writeFn, fileToContent] of byWriteFn) {
      await writeFn(fileToContent);
    }
    emits('saved');
    visible.value = false;
  } catch (e) {
    console.error('[PersonaDialog] Failed to save:', e);
  } finally {
    saving.value = false;
  }
};
</script>

<i18n lang="json">
{
  "zh": {
    "config": {
      "persona": {
        "title": "AI人格",
        "restoreDefault": "恢复默认"
      }
    }
  },
  "en": {
    "config": {
      "persona": {
        "title": "AI Persona",
        "restoreDefault": "Restore Default"
      }
    }
  },
  "ja": {
    "config": {
      "persona": {
        "title": "AI人格",
        "restoreDefault": "デフォルトに戻す"
      }
    }
  },
  "ko": {
    "config": {
      "persona": {
        "title": "AI 페르소나",
        "restoreDefault": "기본값 복원"
      }
    }
  }
}
</i18n>
