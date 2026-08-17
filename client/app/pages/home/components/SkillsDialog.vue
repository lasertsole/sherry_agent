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
              <div v-if="skillTree.length" class="px-4 py-2 overflow-auto shrink-0 border-t border-gray-200 dark:border-gray-700" style="max-height: 26vh;">
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
                <div v-if="!skillTree.length" class="text-sm text-gray-400">{{ t('skills.noFiles') }}</div>
              </div>
              <pre
                v-if="selectedContent !== null"
                class="text-sm whitespace-pre-wrap font-mono m-0 p-4 overflow-auto flex-1 border-t border-gray-200 dark:border-gray-700">
                {{ selectedContent }}
              </pre>
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
import { listSkills, readSkill } from '@/composables/bridge';
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

const onHide = () => {
  selectedSkill.value = null;
  skillDetail.value = null;
  selectedFile.value = null;
  expandedDirs.value = new Set<string>();
  activeCategory.value = 'builtin';
};
</script>
