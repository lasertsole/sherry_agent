<template>
  <Dialog
    v-model:visible="visible"
    :header="t('config.heartbeat.title')"
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
        <!-- Header row: effectiveHint (left) + char counter (right) -->
        <div class="flex items-center justify-between gap-2">
          <div class="flex items-start gap-1 text-xs text-blue-500 dark:text-blue-400">
            <i class="pi pi-info-circle mt-0.5 shrink-0" />
            <span>{{ t('config.heartbeat.effectiveHint') }}</span>
          </div>
          <span
            :class="[
              'text-xs shrink-0',
              totalLength > MAX_CHARS
                ? 'text-red-500'
                : totalLength > MAX_CHARS * 0.9
                  ? 'text-orange-500'
                  : 'text-gray-400'
            ]">
            {{ totalLength }} / {{ MAX_CHARS }}
          </span>
        </div>

        <!-- Active Tasks section -->
        <div class="flex flex-col gap-2">
          <div class="font-semibold text-sm">
            <code class="text-gray-700 dark:text-gray-300">{{ activeSectionTitle }}</code>
            <span class="ml-2 text-xs text-gray-400">({{ activeTasks.length }})</span>
          </div>

          <div
            v-if="!activeTasks.length"
            class="text-sm text-gray-400 dark:text-gray-500 pb-1">
            {{ t('config.heartbeat.activeEmpty') }}
          </div>

          <div
            v-for="(task, i) in activeTasks"
            :key="i"
            class="flex flex-col gap-1">
            <div class="flex items-center justify-end">
              <Button
                :label="t('config.heartbeat.complete')"
                icon="pi pi-check-circle"
                size="small"
                severity="success"
                text
                @click="completeTask(i)" />
              <Button
                :label="t('config.heartbeat.deleteTask')"
                icon="pi pi-trash"
                size="small"
                severity="danger"
                text
                @click="removeTask(i)" />
            </div>
            <Textarea
              v-model="activeTasks[i]"
              class="w-full font-mono text-sm"
              autoResize
              placeholder="..." />
          </div>

          <Button
            :label="t('config.heartbeat.addTask')"
            icon="pi pi-plus"
            severity="secondary"
            outlined
            size="small"
            class="self-start"
            @click="addTask" />
        </div>

        <!-- Completed section (read-only) -->
        <div class="flex flex-col gap-2">
          <div class="font-semibold text-sm">
            <code class="text-gray-500 dark:text-gray-400">{{ completedSectionTitle }}</code>
            <span class="ml-2 text-xs text-gray-400">({{ completedTasks.length }})</span>
          </div>

          <div
            v-if="!completedTasks.length"
            class="text-sm text-gray-400 dark:text-gray-500 pb-1">
            {{ t('config.heartbeat.completedEmpty') }}
          </div>

          <div
            v-for="(task, i) in completedTasks"
            :key="`done-${i}`"
            class="flex items-center justify-between gap-2 bg-gray-100 dark:bg-gray-800/60 rounded-lg px-3 py-2">
            <span class="font-mono text-sm whitespace-pre-wrap text-gray-500 dark:text-gray-400 line-through">{{ task }}</span>
            <div class="flex items-center gap-1 shrink-0">
              <Button
                :label="t('config.heartbeat.reactivate')"
                icon="pi pi-undo"
                size="small"
                severity="secondary"
                text
                @click="reactivateTask(i)" />
              <Button
                :label="t('config.heartbeat.deleteTask')"
                icon="pi pi-trash"
                size="small"
                severity="danger"
                text
                @click="removeCompletedTask(i)" />
            </div>
          </div>
        </div>
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
import { ref, computed, onMounted, onUnmounted } from 'vue';
import { useI18n } from 'vue-i18n';
import { readHeartbeat, writeHeartbeat } from '@/composables/bridge';
import { on, off } from '@/composables/mitt';
import type { Handler } from 'mitt';

const { t } = useI18n();

const props = defineProps<{ modelValue: boolean }>();
const emits = defineEmits<{ 'update:modelValue': [value: boolean]; saved: [] }>();

const visible = computed({
  get: () => props.modelValue,
  set: v => emits('update:modelValue', v)
});

// Task-content character budget (active + completed task texts only).
// The three structural headings (`# Heartbeat Tasks`, `## Active Tasks`,
// `## Completed`) do NOT count toward this limit.
// Mirrors server/service/heartbeat.py heartbeat_content_length.
const MAX_CHARS = 2000;

const ACTIVE_HEADER = '## Active Tasks';
const COMPLETED_HEADER = '## Completed';

const loading = ref(false);
const saving = ref(false);

/** File header region above "## Active Tasks" (fixed, not editable). */
const header = ref('');
/** Live editable active task entries. */
const activeTasks = ref<string[]>([]);
/** Live completed task entries (read-only list). */
const completedTasks = ref<string[]>([]);

/** Snapshot of the three regions at load time, for dirty tracking. */
const originalSnapshot = ref<{ header: string; active: string[]; completed: string[] }>({ header: '', active: [], completed: [] });

const activeSectionTitle = computed(() => `${ACTIVE_HEADER}`);
const completedSectionTitle = computed(() => `${COMPLETED_HEADER}`);

/** Split raw HEARTBEAT.md into the three region lists. */
function parseFile(raw: string): { header: string; active: string[]; completed: string[] } {
  const sections = raw.split(/^##\s+(Active Tasks|Completed)\s*$/m);
  // sections: [header, <markerActive>, activeBlock, <markerCompleted>, completedBlock?, ...]

  let head = sections[0] ?? '';
  let activeBlock = '';
  let completedBlock = '';

  // Walk through the flattened tokens. Header is tokens[0]. Then each
  // occurrence of a marker is followed by the content up to the next marker.
  for (let k = 1; k < sections.length; k++) {
    const marker = sections[k]?.trim();
    const block = sections[k + 1] ?? '';
    if (marker === 'Active Tasks') {
      activeBlock = block;
      k++;
    } else if (marker === 'Completed') {
      completedBlock = block;
      k++;
    }
  }

  const splitTasks = (block: string): string[] =>
    block
      .split(/\n\s*-\s+/)
      .map(e => e.trim())
      .filter(e => e.length > 0);

  return {
    header: head.trim(),
    active: splitTasks(activeBlock),
    completed: splitTasks(completedBlock)
  };
}

/** Rebuild the raw HEARTBEAT.md from the three region lists. */
function serialize(): string {
  const headerText = header.value.trim();
  const actives = activeTasks.value
    .map(t => t.trim())
    .filter(t => t.length > 0)
    .map(t => `- ${t}`);
  const completed = completedTasks.value
    .map(t => t.trim())
    .filter(t => t.length > 0)
    .map(t => `- ${t}`);

  const parts: string[] = [headerText, '', `${ACTIVE_HEADER}`];
  if (actives.length > 0) {
    parts.push('', actives.join('\n\n'));
  }
  parts.push('', `${COMPLETED_HEADER}`);
  if (completed.length > 0) {
    parts.push('', completed.join('\n\n'));
  }

  return parts.join('\n');
}

/** Task-content length: sum of the trimmed task texts (active + completed).
 * The three structural headings (`# Heartbeat Tasks`, `## Active Tasks`,
 * `## Completed`) are excluded, mirroring server heartbeat_content_length. */
const totalLength = computed(() => {
  const activeLen = activeTasks.value.reduce((s, t) => s + t.trim().length, 0);
  const completedLen = completedTasks.value.reduce((s, t) => s + t.trim().length, 0);
  return activeLen + completedLen;
});

function addTask() {
  activeTasks.value.push('');
}

function removeTask(i: number) {
  activeTasks.value.splice(i, 1);
}

/** Move an active task to the completed list. */
function completeTask(i: number) {
  const task = activeTasks.value[i];
  if (task && task.trim().length > 0) {
    activeTasks.value.splice(i, 1);
    completedTasks.value.push(task.trim());
  }
}

/** Move a completed task back to the active list. */
function reactivateTask(i: number) {
  const task = completedTasks.value[i];
  if (task) {
    completedTasks.value.splice(i, 1);
    activeTasks.value.push(task);
  }
}

function removeCompletedTask(i: number) {
  completedTasks.value.splice(i, 1);
}

const loadContent = async () => {
  loading.value = true;
  try {
    const data = await readHeartbeat();
    const parsed = parseFile(data['HEARTBEAT.md'] ?? '');
    header.value = parsed.header;
    activeTasks.value = parsed.active;
    completedTasks.value = parsed.completed;
    originalSnapshot.value = {
      header: parsed.header,
      active: [...parsed.active],
      completed: [...parsed.completed]
    };
  } catch (e) {
    console.error('[HeartbeatDialog] Failed to load content:', e);
  } finally {
    loading.value = false;
  }
};

// The heartbeat backend executes tasks offline and pushes a `heartbeat:updated`
// event over the shared WebSocket (session `default`) whenever the heartbeat
// file changes (e.g. an active task moves to `## Completed`). While this dialog
// is mounted, refresh live so the Completed section stays in sync without a
// manual reload.
type WsFrame = { event?: string; content?: unknown };
let heartbeatWsHandler: Handler | null = null;

function onHeartbeatWsMessage(data: WsFrame) {
  if (data && data.event === 'heartbeat:updated') {
    loadContent();
  }
}

onMounted(() => {
  heartbeatWsHandler = on('ws:message', onHeartbeatWsMessage as Handler);
});

onUnmounted(() => {
  if (heartbeatWsHandler) {
    off('ws:message', heartbeatWsHandler);
    heartbeatWsHandler = null;
  }
});

function isDirty(): boolean {
  const { header: h, active: a, completed: c } = originalSnapshot.value;
  if (header.value !== h) return true;
  if (activeTasks.value.length !== a.length) return true;
  if (completedTasks.value.length !== c.length) return true;
  if (activeTasks.value.some((t, i) => t !== a[i])) return true;
  if (completedTasks.value.some((t, i) => t !== c[i])) return true;
  return false;
}

const canSave = computed(() => {
  if (loading.value || saving.value) return false;
  const len = totalLength.value;
  // Empty task content (only the structural headings) is still a valid,
  // saveable heartbeat file, so only the upper bound applies.
  return len <= MAX_CHARS && isDirty();
});

const handleSave = async () => {
  saving.value = true;
  try {
    await writeHeartbeat({ 'HEARTBEAT.md': serialize() });
    emits('saved');
    visible.value = false;
  } catch (e) {
    console.error('[HeartbeatDialog] Failed to save:', e);
  } finally {
    saving.value = false;
  }
};
</script>
