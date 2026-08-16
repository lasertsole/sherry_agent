<template>
  <Dialog
    v-model:visible="visible"
    :header="t('skills.title')"
    :modal="true"
    :closable="true"
    class="w-[95vw] md:w-[1100px]"
    @show="loadSkills"
    @hide="onHide">
    <div class="flex flex-col gap-3">
      <div v-if="loading" class="flex items-center justify-center py-8">
        <ProgressSpinner style="width: 2rem; height: 2rem" />
      </div>
      <template v-else>
        <div class="flex gap-2 flex-wrap">
          <Button
            v-for="cat in categories"
            :key="cat.key"
            :label="t(cat.i18nKey) + ' (' + (groupedSkills[cat.key]?.length || 0) + ')'"
            :severity="activeCategory === cat.key ? undefined : 'secondary'"
            size="small"
            @click="activeCategory = cat.key" />
        </div>
        <div v-if="activeCategory === 'auto'" class="flex items-center gap-3">
          <Button
            :label="t('skills.tabs.runCurator')"
            :loading="curatorRunning"
            :disabled="curatorRunning"
            icon="pi pi-refresh"
            size="small"
            @click="runCurator" />
          <span v-if="curatorResult" class="text-xs text-gray-500 dark:text-gray-400">{{ curatorResult }}</span>
          <span v-else-if="curatorError" class="text-xs text-red-500 dark:text-red-400">{{ curatorError }}</span>
        </div>
        <div class="flex gap-3" style="min-height: 60vh;">
          <div class="w-64 shrink-0 overflow-auto flex flex-col gap-1 pr-1" style="max-height: 72vh;">
            <div
              v-for="skill in currentSkills"
              :key="skill.location"
              :class="[
                'px-3 py-1.5 rounded-lg cursor-pointer text-xs leading-snug break-all border border-transparent',
                selectedSkill?.location === skill.location
                  ? 'bg-primary-50 dark:bg-primary-900/20 border-primary-200 dark:border-primary-700'
                  : 'hover:bg-gray-50 dark:hover:bg-gray-800'
              ]"
              @click="selectSkill(skill)"
              :title="skill.name">
              {{ skill.name }}
            </div>
            <div v-if="currentSkills.length === 0" class="text-sm text-gray-400 px-3 py-2">
              {{ t('skills.empty') }}
            </div>
          </div>
          <div class="flex-1 overflow-auto rounded-lg bg-gray-50 dark:bg-gray-800/50 p-4" style="max-height: 72vh;">
            <div v-if="detailLoading" class="flex items-center justify-center h-full">
              <ProgressSpinner style="width: 2rem; height: 2rem" />
            </div>
            <template v-else-if="skillDetail">
              <div class="flex items-center justify-between mb-3">
                <div class="flex flex-col">
                  <span class="text-lg font-bold">{{ skillDetail.name }}</span>
                  <span v-if="skillDetail.description" class="text-sm text-gray-500 dark:text-gray-400">{{ skillDetail.description }}</span>
                </div>
                <div class="flex items-center gap-2">
                  <Tag
                    v-if="isReadonly(activeCategory)"
                    :value="t('skills.readonly')"
                    severity="warn"
                    size="small" />
                  <Tag
                    :value="t(categoryI18nKey(activeCategory))"
                    severity="info"
                    size="small" />
                </div>
              </div>
              <pre v-if="skillDetail.content" class="text-sm whitespace-pre-wrap font-mono m-0">{{ stripFrontmatter(skillDetail.content) }}</pre>
            </template>
            <div v-else class="flex items-center justify-center h-full text-sm text-gray-400">
              {{ t('skills.selectHint') }}
            </div>
          </div>
        </div>
      </template>
    </div>
  </Dialog>
</template>

<script lang="ts" setup>
import { ref, computed } from 'vue';
import { useI18n } from 'vue-i18n';
import dayjs from 'dayjs';
import { listSkills, readSkill, runCuratorReview } from '@/composables/bridge';
import type { SkillInfo, SkillDetail } from '@/composables/bridge';

const { t } = useI18n();

const props = defineProps<{ modelValue: boolean }>();
const emits = defineEmits<{ 'update:modelValue': [value: boolean] }>();

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emits('update:modelValue', v),
});

const categories = [
  { key: 'builtin' as const, i18nKey: 'skills.tabs.builtin' },
  { key: 'auto' as const, i18nKey: 'skills.tabs.auto' },
  { key: 'third_party' as const, i18nKey: 'skills.tabs.thirdParty' },
];

const loading = ref(false);
const detailLoading = ref(false);
const allSkills = ref<SkillInfo[]>([]);
const activeCategory = ref<'builtin' | 'auto' | 'third_party'>('builtin');
const selectedSkill = ref<SkillInfo | null>(null);
const skillDetail = ref<SkillDetail | null>(null);
const curatorRunning = ref(false);
const curatorResult = ref('');
const curatorError = ref('');

const groupedSkills = computed(() => {
  const groups: Record<string, SkillInfo[]> = { builtin: [], auto: [], third_party: [] };
  for (const s of allSkills.value) {
    const cat = s.category;
    if (groups[cat]) groups[cat].push(s);
    else groups.third_party.push(s);
  }
  return groups;
});

const currentSkills = computed(() => groupedSkills.value[activeCategory.value] ?? []);

const isReadonly = (category: string) => category === 'builtin' || category === 'auto';

const categoryI18nKey = (category: string) => {
  const found = categories.find((c) => c.key === category);
  return found ? found.i18nKey : 'skills.tabs.thirdParty';
};

const stripFrontmatter = (content: string) => {
  if (content.startsWith('---')) {
    const parts = content.split('---', 3);
    if (parts.length >= 3) return parts[2].trim();
  }
  return content;
};

const loadSkills = async () => {
  loading.value = true;
  try {
    const resp = await listSkills();
    allSkills.value = resp.skills ?? [];
  } catch (e) {
    console.error('[SkillsDialog] Failed to load skills:', e);
  } finally {
    loading.value = false;
  }
};

const selectSkill = async (skill: SkillInfo) => {
  selectedSkill.value = skill;
  skillDetail.value = null;
  detailLoading.value = true;
  try {
    skillDetail.value = await readSkill(skill.location);
  } catch (e) {
    console.error('[SkillsDialog] Failed to read skill:', e);
  } finally {
    detailLoading.value = false;
  }
};

const runCurator = async () => {
  if (curatorRunning.value) return;
  curatorRunning.value = true;
  curatorResult.value = '';
  curatorError.value = '';
  try {
    const resp = await runCuratorReview();
    if (resp.success && resp.result) {
      const r = resp.result;
      const total = Object.values(r.auto_transitions).reduce((sum, n) => sum + n, 0);
      const startedAt = dayjs(r.started_at).isValid()
        ? dayjs(r.started_at).format('YYYY-MM-DD HH:mm')
        : r.started_at;
      curatorResult.value = `${t('skills.tabs.curatorDone')} ${startedAt} · ${total} ${t('skills.tabs.curatorTransitions')}${r.summary_so_far ? ` · ${r.summary_so_far}` : ''}`;
    } else {
      curatorError.value = resp.error || t('skills.tabs.curatorFailed');
    }
  } catch (e) {
    console.error('[SkillsDialog] Curator run failed:', e);
    curatorError.value = t('skills.tabs.curatorFailed');
  } finally {
    curatorRunning.value = false;
  }
  loadSkills();
};

const onHide = () => {
  selectedSkill.value = null;
  skillDetail.value = null;
  activeCategory.value = 'builtin';
  curatorRunning.value = false;
  curatorResult.value = '';
  curatorError.value = '';
};
</script>
