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
            :key="tab.file"
            :header="t(tab.i18nKey)">
            <div class="flex flex-col gap-2">
              <div class="flex items-center justify-between">
                <span class="text-sm text-gray-500 dark:text-gray-400">{{ t(tab.i18nDescKey) }}</span>
                <span
                  :class="[
                    'text-xs',
                    (editContent[tab.file]?.length ?? 0) > MAX_CHARS
                      ? 'text-red-500'
                      : (editContent[tab.file]?.length ?? 0) > MAX_CHARS * 0.9
                        ? 'text-orange-500'
                        : 'text-gray-400'
                  ]">
                  {{ editContent[tab.file]?.length ?? 0 }} / {{ MAX_CHARS }}
                </span>
              </div>
              <Textarea
                v-model="editContent[tab.file]"
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
import { readSystemPrompt, writeSystemPrompt } from '@/composables/bridge';

const { t } = useI18n();

const props = defineProps<{ modelValue: boolean }>();
const emits = defineEmits<{ 'update:modelValue': [value: boolean]; saved: [] }>();

const visible = computed({
  get: () => props.modelValue,
  set: v => emits('update:modelValue', v)
});

const MAX_CHARS = 2000;

const tabs = [
  { file: 'AGENTS.md', i18nKey: 'config.tabs.agents', i18nDescKey: 'config.desc.agents' },
  { file: 'IDENTITY.md', i18nKey: 'config.tabs.identity', i18nDescKey: 'config.desc.identity' },
  { file: 'SOUL.md', i18nKey: 'config.tabs.soul', i18nDescKey: 'config.desc.soul' },
  { file: 'USER.md', i18nKey: 'config.tabs.user', i18nDescKey: 'config.desc.user' }
] as const;

const activeTab = ref(0);
const loading = ref(false);
const saving = ref(false);
const editContent = ref<Record<string, string>>({});
const originalContent = ref<Record<string, string>>({});

const loadContent = async () => {
  loading.value = true;
  try {
    const promptData = await readSystemPrompt();
    const content: Record<string, string> = {};
    for (const tab of tabs) {
      content[tab.file] = promptData[tab.file] ?? '';
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
  const file = tabs[activeTab.value]?.file;
  if (!file) return false;
  const len = editContent.value[file]?.length ?? 0;
  return len > 0 && len <= MAX_CHARS;
});

const handleSave = async () => {
  saving.value = true;
  try {
    const fileToContent: Record<string, string> = {};
    for (const tab of tabs) {
      const content = editContent.value[tab.file] ?? '';
      if (content !== (originalContent.value[tab.file] ?? '')) {
        fileToContent[tab.file] = content;
      }
    }
    if (Object.keys(fileToContent).length > 0) {
      await writeSystemPrompt(fileToContent);
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
