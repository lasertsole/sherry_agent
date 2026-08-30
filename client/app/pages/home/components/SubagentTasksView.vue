<template>
  <div class="flex flex-col flex-1 h-full bg-transparent dark:bg-transparent">
    <!-- Top toolbar: back to the caller session (single item, locating the requester_session_key of the current subtree root (depth-1 task)) -->
    <div
      class="flex items-center justify-between gap-3 shrink-0 border-b border-solid border-gray-light dark:border-gray-dark bg-white/60 dark:bg-[#1a1d21]/60 px-4 py-2">
      <div class="min-w-0 flex items-center gap-3">
        <button
          v-if="backToSessionSid"
          type="button"
          class="shrink-0 flex items-center gap-1.5 text-xs text-primary cursor-pointer hover:opacity-80 transition-opacity"
          :title="t('sidebar.backToSessionPrompt')"
          @click="jumpBackToSession">
          <i class="pi pi-arrow-left text-xs" />
          <span>{{ t('sidebar.backToSession') }}</span>
        </button>
        <div class="min-w-0 flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
          <i class="pi pi-sitemap text-gray-400 dark:text-gray-500" />
          <span class="truncate">{{ currentSessionId }}</span>
        </div>
      </div>
      <div class="shrink-0 flex items-center gap-3">
        <button
          type="button"
          class="shrink-0 flex items-center gap-1.5 text-xs text-primary cursor-pointer hover:opacity-80 transition-opacity"
          :class="{ 'opacity-60 pointer-events-none': taskLoading }"
          :title="t('sidebar.refreshGraphPrompt')"
          @click="handleRefresh">
          <i
            class="pi text-xs"
            :class="taskLoading ? 'pi-spin pi-spinner' : 'pi-refresh'" />
          <span>{{ t('sidebar.refreshGraph') }}</span>
        </button>
      </div>
    </div>

    <!-- Main body: tree on top, details below (two sections) -->
    <div class="flex flex-col flex-1 min-h-0">
      <!-- Upper half: full tree graph (branching from root node down to leaves) -->
      <div class="flex-[3] min-h-0">
        <!-- Loading -->
        <div
          v-if="taskLoading && focusedSubtreeRuns.length === 0"
          class="flex flex-col items-center justify-center gap-3 h-full py-16">
          <i class="pi pi-spin pi-spinner text-3xl text-gray-400 dark:text-gray-500" />
          <span class="text-sm text-gray-500 dark:text-gray-400">{{ t('sidebar.tasksLoading') }}</span>
        </div>

        <!-- Empty state -->
        <div
          v-else-if="focusedSubtreeRuns.length === 0"
          class="flex flex-col items-center justify-center gap-4 h-full py-16">
          <i class="pi pi-sitemap text-6xl text-gray-300 dark:text-gray-600" />
          <div class="text-base text-gray-500 dark:text-gray-400">{{ t('sidebar.noTasks') }}</div>
        </div>

        <!-- Tree graph -->
        <SubagentFlowGraph
          v-else
          class="h-full w-full"
          v-model:current-session-id="currentSessionId"
          v-model:selected-run-id="selectedRunId"
          v-model:selected-run="selectedRun"
          :display-runs="focusedRunId ? focusedSubtreeRuns : undefined" />
      </div>

      <!-- Lower half: details of the selected node -->
      <div
        class="flex-[2] min-h-0 border-t border-solid border-gray-light dark:border-gray-dark bg-white/60 dark:bg-[#1a1d21]/60">
        <SubagentRunDetail :run="selectedRun" />
      </div>
    </div>
  </div>
</template>

<script lang="ts" setup>
import { computed, onMounted, onUnmounted } from 'vue';
import { useI18n } from 'vue-i18n';
import { useRoute, useRouter } from 'vue-router';
import { on, off, emit as busEmit } from '@/composables/mitt';
import { useSubagentTasks } from '@/composables/useSubagentTasks';
import type { SubagentRun } from '@/composables/bridge';
import SubagentFlowGraph from './SubagentFlowGraph.vue';
import SubagentRunDetail from './SubagentRunDetail.vue';

const props = defineProps<{
  /** run_id to initially locate/expand (passed in when clicking a sidebar task item) */
  initialRunId?: string;
}>();

const { t } = useI18n();
const route = useRoute();
const router = useRouter();
const localePath = useLocalePath();

const {
  focusedSubtreeRuns,
  taskLoading,
  focusedRunId,
  selectedRunId,
  focusRun,
  initTasks,
  setTasksTabActive,
  refreshFocusedSubtree,
  // Shares one session-existence check with orphaned-run filtering (avoids the two sides fetching and normalizing inconsistently)
  normalizeSessionKey,
  loadSubagentValidSessions,
  validSessionIds
} = useSubagentTasks();

/** Current session id (used by the flow graph to fetch this session's run tree) */
const currentSessionId = computed(() => {
  const sid = route.params.sid;
  return typeof sid === 'string' && sid ? sid : undefined;
});

/**
 * The full run object of the selected node, displayed by the detail panel below.
 *
 * [Design note] This is a "writable computed" rather than a plain ref:
 *  - The getter derives from the singleton selectedRunId (written by both focusRun and
 *    flow-graph node clicks) plus the focused subtree pool, so after switching task boxes
 *    (focusRun only writes selectedRunId) the detail panel **automatically** syncs to that
 *    task box's default active task root node, without depending on watch timing.
 *  - Compared with watch(focusedRunId): when the component mounts, focusedRunId/selectedRunId
 *    have already been written by the sidebar before navigation (the value exists before this
 *    component mounts); a plain watch does not fire for "already set" values, which would leave
 *    the detail panel empty on first render/after mounting. A computed is evaluated reactively,
 *    so as soon as the pool (focusedSubtreeRuns) fills after mounting it recomputes immediately.
 *  - The setter supports the v-model:selected-run write-back from SubagentFlowGraph node
 *    clicks (two-way sync), and also syncs selectedRunId to keep the highlight and the
 *    detail source consistent.
 */
const selectedRun = computed<SubagentRun | undefined>({
  get: () => {
    const id = selectedRunId.value;
    if (id) {
      const inSubtree = focusedSubtreeRuns.value.find(r => r.run_id === id);
      if (inSubtree) return inSubtree;
    }
    return undefined;
  },
  set: val => {
    if (val) selectedRunId.value = val.run_id;
  }
});

/** Target session_id for "back to session":
 *  Prefers the requester_session_key of the current focused subtree root (depth-1 task),
 *  i.e. the parent session that spawned it, normalized to a bare UUID, and returns it only
 *  when that session actually exists in the live session list (server-authoritative or local
 *  placeholder), preventing "back to session" from jumping to a destroyed/nonexistent session
 *  (the ghost button of a stale/orphaned run);
 *  when not focused (default full list of depth-1 tasks across multiple sessions), falls back
 *  to the current route's sid.
 *  The existence check reuses the composable's shared validSessionIds/normalizeSessionKey.
 *  Note: returns undefined when the target session does not exist (orphaned run, caller session
 *  destroyed), which hides the button, while the run's task box still shows in the list —
 *  following the "orphan runs must be shown, but must not offer a back-to-session button"
 *  constraint. */
const backToSessionSid = computed(() => {
  // The target is always based on the current route's sid (bare UUID); navigation uses the normalized key
  const fallback = normalizeSessionKey(currentSessionId.value);
  let candidate: string | null | undefined;
  if (focusedRunId.value && focusedSubtreeRuns.value.length > 0) {
    const root = focusedSubtreeRuns.value[0];
    candidate = normalizeSessionKey(root?.requester_session_key);
  }
  const target = candidate || fallback;
  // Only show/enable "back to session" when the target session actually exists in the live session list
  if (!target || !validSessionIds.value.has(target)) return undefined;
  return target;
});

/** Jump to the caller session's chat page:
 *  Target session = the requester_session_key of the current focused subtree root (depth-1 task),
 *  i.e. the parent session that spawned this subtask, not the current route's sid;
 *  when not focused (default full list), falls back to the current sid.
 *  Embedded view (viewMode==='tasks' inside [sid].vue): broadcasts 'subagent:show-chat' over the
 *  mitt bus; the onShowChat listener in [sid].vue switches viewMode back to 'chat', and the
 *  sidebar switches back to the "Sessions" tab accordingly.
 *  Standalone page /home/tasks/{sid}: there is no 'subagent:show-chat' listener, so an explicit
 *  route to that session's chat page is required.
 *  Double-invocation safe: in embedded mode, when the target session is the current route,
 *  router.push is a no-op and no duplicate navigation occurs. */
const jumpBackToSession = () => {
  const targetSid = backToSessionSid.value;
  if (!targetSid) return;
  // Embedded (viewMode==='tasks' inside [sid].vue): notify the host to switch back to the chat view
  busEmit('subagent:show-chat');
  // Switch the sidebar back to the "Sessions" tab, ensuring the session list is visible and the target session is highlighted
  setTasksTabActive(false);
  // Route to the target session page; a no-op in embedded mode if already on the target page, completes the jump on the standalone page.
  // The route change triggers SessionSidebar's activeSessionId watch, moving the highlight to the target session.
  router.push(localePath(`/home/${targetSid}`));
};

/** Received the sidebar's "show background tasks" event: locate/expand the specified run (if any) */
const onShowTasks = (event: unknown) => {
  const runId = typeof event === 'string' ? event : undefined;
  focusRun(runId);
};

/** Manually refresh the flow graph: refresh only that subtree when a task box is focused, otherwise do a full refresh for the current session */
const handleRefresh = () => {
  refreshFocusedSubtree();
};

onMounted(() => {
  initTasks(currentSessionId.value);
  // Preload the set of existing sessions for "back to session" target existence validation (hides ghost buttons)
  // Reuses the composable's shared loader (already triggered inside initTasks; idempotent here as a safeguard, covering the case where only this view is mounted)
  void loadSubagentValidSessions();
  on('subagent:show-tasks', onShowTasks);
  // On first entry with an initialRunId, locate/expand the corresponding run (state is hoisted to a singleton and survives remounts)
  focusRun(props.initialRunId);
});

onUnmounted(() => {
  off('subagent:show-tasks', onShowTasks);
});
</script>
