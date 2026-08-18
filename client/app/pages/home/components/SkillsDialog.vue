<template>
  <Dialog
    v-model:visible="visible"
    :header="t('skills.title')"
    :modal="true"
    :closable="true"
    class="w-[95vw] md:w-[1400px]"
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
        <div class="flex gap-3" style="min-height: 60vh;">
          <div class="w-64 shrink-0 flex flex-col gap-1 pr-1">
            <div v-if="activeCategory === 'auto'" class="flex items-center gap-2 pb-1">
              <Button
                :label="t('skills.tabs.runCurator')"
                :loading="curatorRunning"
                :disabled="curatorRunning"
                icon="pi pi-refresh"
                size="small"
                @click="runCurator" />
              <Button
                v-tooltip.right="t('skills.tabs.runCuratorHint')"
                icon="pi pi-question-circle"
                severity="secondary"
                text
                rounded
                size="small"
                :aria-label="t('skills.tabs.runCurator')"
                class="p-button-icon-only" />
              <span v-if="curatorResult" class="text-xs text-gray-500 dark:text-gray-400">{{ curatorResult }}</span>
              <span v-else-if="curatorError" class="text-xs text-red-500 dark:text-red-400">{{ curatorError }}</span>
            </div>
            <div v-if="activeCategory === 'third_party'" class="flex flex-col gap-1 pb-1">
              <div class="flex items-center gap-2">
                <Button
                  :label="t('skills.tabs.uploadSkill')"
                  :loading="uploading"
                  :disabled="uploading"
                  icon="pi pi-upload"
                  size="small"
                  @click="fileInput?.click()" />
                <input
                  :key="fileInputKey"
                  ref="fileInput"
                  type="file"
                  accept=".md,text/markdown"
                  class="hidden"
                  @change="handleUpload" />
              </div>
              <span v-if="uploadStatus" class="text-xs text-green-600 dark:text-green-400">{{ uploadStatus }}</span>
              <span v-else-if="uploadError" class="text-xs text-red-500 dark:text-red-400">{{ uploadError }}</span>
              <span v-else-if="toggleError" class="text-xs text-red-500 dark:text-red-400">{{ toggleError }}</span>
              <span v-else class="text-xs text-gray-400 dark:text-gray-500">{{ t('skills.tabs.uploadSkillHint') }}</span>
              <ul
                v-if="uploadWarnings.length"
                class="mt-1 flex flex-col gap-0.5"
                role="alert"
                aria-live="polite">
                <li
                  v-for="warn in uploadWarnings"
                  :key="warn"
                  class="text-xs text-amber-600 dark:text-amber-400">
                  {{ warn }}
                </li>
              </ul>
            </div>
            <div class="overflow-auto flex flex-col gap-1 pr-1" style="max-height: 72vh;">
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
                <div class="flex items-center justify-between gap-2">
                  <span class="min-w-0 break-all">{{ skill.name }}</span>
                  <ToggleSwitch
                    v-if="activeCategory === 'third_party'"
                    :model-value="(skill as SkillInfo & { active?: boolean }).active ?? false"
                    size="small"
                    @update:model-value="toggleActive(skill as SkillInfo & { active?: boolean }, $event as boolean)" />
                </div>
              </div>
              <div v-if="currentSkills.length === 0" class="text-sm text-gray-400 px-3 py-2">
                {{ t('skills.empty') }}
              </div>
            </div>
          </div>
          <div class="flex-1 flex flex-col overflow-hidden rounded-lg bg-gray-50 dark:bg-gray-800/50" style="max-height: 72vh;">
            <div v-if="detailLoading" class="flex items-center justify-center h-full">
              <ProgressSpinner style="width: 2rem; height: 2rem" />
            </div>
            <template v-else-if="skillDetail">
              <div class="flex items-center justify-between p-4 pb-2 shrink-0">
                <div class="flex flex-col">
                  <span class="text-lg font-bold">{{ skillDetail.name }}</span>
                  <span v-if="skillDetail.description" class="text-sm text-gray-500 dark:text-gray-400">{{ skillDetail.description }}</span>
                </div>
                <div class="flex items-center gap-2 shrink-0">
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
              <div class="flex flex-1 overflow-hidden border-t border-gray-200 dark:border-gray-700">
                <div
                  v-if="skillTree.length"
                  class="w-56 shrink-0 overflow-auto border-r border-gray-200 dark:border-gray-700 py-2"
                  style="max-height: 72vh;">
                  <div
                    v-for="row in skillTree"
                    :key="row.key"
                    class="flex items-center gap-1 py-0.5 cursor-pointer select-none rounded px-1"
                    :class="[
                      'text-xs',
                      selectedFile?.path === row.node.path
                        ? 'bg-primary-50 dark:bg-primary-900/30 text-primary-600 dark:text-primary-300'
                        : 'hover:bg-gray-100 dark:hover:bg-gray-700/50'
                    ]"
                    :style="{ paddingLeft: 4 + row.depth * 16 + 'px' }"
                    @click="selectFile(row.node)">
                    <i
                      :class="row.node.type === 'dir' ? (expandedDirs.has(row.node.path) ? 'pi pi-folder-open' : 'pi pi-folder') : fileIcon(row.node.name)"
                      class="text-xs"
                      :style="row.node.type === 'dir' ? { color: '#f0b429' } : { color: '#6366f1' }" />
                    <span class="font-mono whitespace-pre">{{ row.node.name }}</span>
                  </div>
                  <div v-if="!skillTree.length" class="text-sm text-gray-400 px-3 py-2">{{ t('skills.noFiles') }}</div>
                </div>
                <pre
                  v-if="selectedContent !== null"
                  class="text-sm whitespace-pre-wrap font-mono m-0 p-4 overflow-auto flex-1"
                  style="max-height: 72vh;">
                  {{ selectedContent }}
                </pre>
              </div>
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
import { listSkills, readSkill, runCuratorReview, uploadSkill, setSkillActive } from '@/composables/bridge';
import type { SkillInfo, SkillDetail, SkillFileNode } from '@/composables/bridge';

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
const uploading = ref(false);
const uploadStatus = ref('');
const uploadError = ref('');
const uploadWarnings = ref<string[]>([]);
const toggleError = ref('');
const fileInputKey = ref(0);
const fileInput = ref<HTMLInputElement | null>(null);

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

/** Nested representation of a skill's directory structure. */
interface TreeNode {
  node: SkillFileNode;
  children: TreeNode[];
}

/** Build a nested tree from the backend's flat `files[]` listing (depth-first order). */
const buildTree = (files: SkillFileNode[]): TreeNode[] => {
  // Build a proper nested tree by accumulating each node on its parent path.
  const byPath = new Map<string, TreeNode>();
  const roots: TreeNode[] = [];
  for (const f of files) {
    const tn: TreeNode = { node: f, children: [] };
    byPath.set(f.path, tn);
  }
  for (const f of files) {
    const tn = byPath.get(f.path)!;
    const parts = f.path.split('/');
    if (parts.length === 1) {
      roots.push(tn);
    } else {
      const parentPath = parts.slice(0, -1).join('/');
      const parent = byPath.get(parentPath);
      if (parent) {
        parent.children.push(tn);
      } else {
        roots.push(tn); // safety: orphan node at root
      }
    }
  }
  return roots;
};

/** Flatten the nested tree into display rows with depth + expansion state. */
interface TreeRow {
  key: string;
  node: SkillFileNode;
  depth: number;
}

const expandedDirs = ref<Set<string>>(new Set<string>());
const selectedFile = ref<SkillFileNode | null>(null);

const skillTree = computed<TreeRow[]>(() => {
  if (!skillDetail.value?.files?.length) return [];
  const rows: TreeRow[] = [];
  const walk = (nodes: TreeNode[], depth: number) => {
    for (const tn of nodes) {
      rows.push({ key: tn.node.path, node: tn.node, depth });
      if (tn.node.type === 'dir' && expandedDirs.value.has(tn.node.path)) {
        walk(tn.children, depth + 1);
      }
    }
  };
  walk(buildTree(skillDetail.value.files), 0);
  return rows;
});

const selectFile = (node: SkillFileNode) => {
  if (node.type === 'dir') {
    const next = new Set(expandedDirs.value);
    if (next.has(node.path)) next.delete(node.path);
    else next.add(node.path);
    expandedDirs.value = next;
    return;
  }
  selectedFile.value = node;
};

const fileIcon = (name: string) => {
  if (name.endsWith('.md')) return 'pi pi-book';
  if (/\.(py|js|ts|tsx|jsx|rs|go|java|c|cpp|sh|ps1)$/.test(name)) return 'pi pi-code';
  if (name.endsWith('.json')) return 'pi pi-database';
  if (name.endsWith('.yaml') || name.endsWith('.yml')) return 'pi pi-sliders-h';
  return 'pi pi-file';
};

const selectedContent = computed(() => {
  if (!selectedFile.value) return null;
  if (selectedFile.value.path === 'SKILL.md') return stripFrontmatter(skillDetail.value?.content ?? '');
  return selectedFile.value.content ?? null;
});

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

const handleUpload = async (event: Event) => {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  // Reset the input value so re-selecting the same file re-triggers onchange.
  input.value = '';
  if (!file) return;
  uploading.value = true;
  uploadStatus.value = '';
  uploadError.value = '';
  uploadWarnings.value = [];
  try {
    const resp = await uploadSkill(file);
    if (resp.success) {
      uploadStatus.value = t('skills.uploadSuccess');
      // Surface CAUTION scanner warnings (advisory only) without blocking the
      // accepted upload. Warnings like "flagged by security scanner" let the
      // user decide whether to keep/activate the skill.
      uploadWarnings.value = Array.isArray(resp.warnings) ? resp.warnings : [];
      await loadSkills();
    } else {
      uploadError.value = resp.message || t('skills.uploadFailed');
    }
  } catch (e) {
    console.error('[SkillsDialog] Upload failed:', e);
    uploadError.value = t('skills.uploadFailed');
  } finally {
    uploading.value = false;
  }
};

const toggleActive = async (skill: SkillInfo & { active?: boolean }, value: boolean) => {
  const prev = skill.active ?? false;
  skill.active = value;
  toggleError.value = '';
  try {
    const resp = await setSkillActive(skill.name, value);
    if (!resp.success) {
      skill.active = prev;
      toggleError.value = resp.message || t('skills.toggleFailed');
    }
  } catch (e) {
    console.error('[SkillsDialog] Toggle failed:', e);
    skill.active = prev;
    toggleError.value = t('skills.toggleFailed');
  }
};

const selectSkill = async (skill: SkillInfo) => {
  selectedSkill.value = skill;
  skillDetail.value = null;
  selectedFile.value = null;
  expandedDirs.value = new Set<string>();
  detailLoading.value = true;
  try {
    const detail = await readSkill(skill.location);
    skillDetail.value = detail;
    // Default the preview to SKILL.md (the skill's primary entry point).
    const rootMd = detail.files?.find((f) => f.path === 'SKILL.md') ?? null;
    selectedFile.value = rootMd ?? null;
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
  selectedFile.value = null;
  expandedDirs.value = new Set<string>();
  activeCategory.value = 'builtin';
  curatorRunning.value = false;
  curatorResult.value = '';
  curatorError.value = '';
  uploading.value = false;
  uploadStatus.value = '';
  uploadError.value = '';
  uploadWarnings.value = [];
  toggleError.value = '';
};
</script>
