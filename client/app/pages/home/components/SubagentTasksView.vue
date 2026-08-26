<template>
  <div class="flex flex-col flex-1 h-full bg-transparent dark:bg-transparent">
    <!-- 顶部工具条：返回调用方会话（单条，定位到当前子树根（深度1 task）的 requester_session_key） -->
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

    <!-- 主体：上树下详（两栏） -->
    <div class="flex flex-col flex-1 min-h-0">
      <!-- 上半：总树状图（从根节点分叉到叶子） -->
      <div class="flex-[3] min-h-0">
        <!-- 加载中 -->
        <div
          v-if="taskLoading && focusedSubtreeRuns.length === 0"
          class="flex flex-col items-center justify-center gap-3 h-full py-16">
          <i class="pi pi-spin pi-spinner text-3xl text-gray-400 dark:text-gray-500" />
          <span class="text-sm text-gray-500 dark:text-gray-400">{{ t('sidebar.tasksLoading') }}</span>
        </div>

        <!-- 空态 -->
        <div
          v-else-if="focusedSubtreeRuns.length === 0"
          class="flex flex-col items-center justify-center gap-4 h-full py-16">
          <i class="pi pi-sitemap text-6xl text-gray-300 dark:text-gray-600" />
          <div class="text-base text-gray-500 dark:text-gray-400">{{ t('sidebar.noTasks') }}</div>
        </div>

        <!-- 树状图 -->
        <SubagentFlowGraph
          v-else
          class="h-full w-full"
          v-model:current-session-id="currentSessionId"
          v-model:selected-run-id="selectedRunId"
          v-model:selected-run="selectedRun"
          :display-runs="focusedRunId ? focusedSubtreeRuns : undefined" />
      </div>

      <!-- 下半：选中节点详情 -->
      <div class="flex-[2] min-h-0 border-t border-solid border-gray-light dark:border-gray-dark bg-white/60 dark:bg-[#1a1d21]/60">
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
  /** 初始要定位/展开的 run_id（点击侧边栏任务项时传入） */
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
  // 与 orphaned-run 过滤共享一套会话存在性判定（避免两端各自拉取、各自归一化不一致）
  normalizeSessionKey,
  loadSubagentValidSessions,
  validSessionIds
} = useSubagentTasks();

/** 当前会话 id（供流程图拉取该会话的运行树） */
const currentSessionId = computed(() => {
  const sid = route.params.sid;
  return typeof sid === 'string' && sid ? sid : undefined;
});

/**
 * 选中节点的完整 run 对象，供下方详情面板展示。
 *
 * 【设计说明】这是一个「可写 computed」而非普通 ref：
 *  - getter 从单例 selectedRunId（focusRun/流程图节点点击都会写它）+ 聚焦子树池派生，
 *    因此切换 task box（focusRun 仅写 selectedRunId）后，详情栏会**自动**同步为该
 *    task box 对应、默认激活的任务根节点，无需依赖 watch 时序。
 *  - 相比 watch(focusedRunId)：组件挂载时 focusedRunId/selectedRunId 已由侧边栏在导航前
 *    写入（本组件 mount 前该值已存在），普通 watch 不会对“已有值”触发，导致首屏/挂载后
 *    详情栏为空。computed 是响应式求值，挂载后池子（focusedSubtreeRuns）一填充便立即重算。
 *  - setter 兼容 SubagentFlowGraph 节点点击的 v-model:selected-run 写回（两向同步），
 *    同时把 selectedRunId 一并同步，保持高亮与详情源一致。
 */
const selectedRun = computed<SubagentRun | undefined>({
  get: () => {
    const id = selectedRunId.value;
    if (id) {
      const inSubtree = focusedSubtreeRuns.value.find((r) => r.run_id === id);
      if (inSubtree) return inSubtree;
    }
    return undefined;
  },
  set: (val) => {
    if (val) selectedRunId.value = val.run_id;
  }
});

/** 「返回会话」的目标 session_id：
 *  优先取当前聚焦子树根（深度1 task）的 requester_session_key（发起它的父会话），
 *  归一化为 bare UUID，并仅当该会话在（服务端权威/本地占位的）实时会话列表中真实存在时才返回，
 *  从而避免「返回会话」跳到已被销毁/不存在的会话（stale/orphaned run 的 ghost 按钮）；
 *  未聚焦（默认全量列表，跨多会话的深度1任务）时回退为当前 route 的 sid。
 *  存在性判定复用 composable 共享的 validSessionIds/normalizeSessionKey。
 *  注意：目标会话不存在（orphaned run，调用方会话已销毁）时返回 undefined，从而隐藏按钮，
 *  但该 run 的任务 box 仍在列表中展示——遵循「孤儿 run 要显示，但不能有返回会话」的约束。 */
const backToSessionSid = computed(() => {
  // 目标始终基于当前 route 的 sid（bare UUID），导航用归一化键
  const fallback = normalizeSessionKey(currentSessionId.value);
  let candidate: string | null | undefined;
  if (focusedRunId.value && focusedSubtreeRuns.value.length > 0) {
    const root = focusedSubtreeRuns.value[0];
    candidate = normalizeSessionKey(root?.requester_session_key);
  }
  const target = candidate || fallback;
  // 仅当目标会话在实时会话列表中真实存在时才展示/启用「返回会话」
  if (!target || !validSessionIds.value.has(target)) return undefined;
  return target;
});

/** 跳到调用方 Session 的聊天页：
 *  目标会话 = 当前聚焦子树根（深度1 task）的 requester_session_key（发起该子任务的父会话），
 *  而非当前 route 的 sid；未聚焦（默认全量列表）时回退为当前 sid。
 *  嵌入式视图（viewMode==='tasks' 于 [sid].vue 内）：通过 mitt 总线广播 'subagent:show-chat'，
 *  由 [sid].vue 的 onShowChat 监听器把 viewMode 切回 'chat'，侧边栏也随之切回「会话」标签。
 *  独立页 /home/tasks/{sid}：无 'subagent:show-chat' 监听者，需显式路由到该会话聊天页。
 *  双重调用安全：嵌入式下目标会话即当前 route 时 router.push 为 no-op，不会重复导航。 */
const jumpBackToSession = () => {
  const targetSid = backToSessionSid.value;
  if (!targetSid) return;
  // 嵌入式（viewMode==='tasks' 于 [sid].vue 内）：通知宿主切回聊天视图
  busEmit('subagent:show-chat');
  // 侧边栏切回「会话」标签，确保会话列表可见并高亮目标 session
  setTasksTabActive(false);
  // 路由到目标会话页；嵌入式下若已在目标页则为 no-op，独立页则完成跳转。
  // 路由变化会触发 SessionSidebar 的 activeSessionId watch，从而把高亮切到目标 session。
  router.push(localePath(`/home/${targetSid}`));
};

/** 收到侧边栏「展示后台任务」事件：定位/展开指定 run（若有） */
const onShowTasks = (runId?: string) => {
  focusRun(runId);
};

/** 手动刷新流程图：有聚焦 task box 时仅刷新该子树，否则按当前会话全量刷新 */
const handleRefresh = () => {
  refreshFocusedSubtree();
};

onMounted(() => {
  initTasks(currentSessionId.value);
  // 预载「存在会话」集合，用于「返回会话」目标的存在性校验（隐藏 ghost 按钮）
  // 复用 composable 共享加载器（initTasks 内已触发，此处幂等预留，兼顾仅本视图挂载的场景）
  void loadSubagentValidSessions();
  on('subagent:show-tasks', onShowTasks);
  // 首次进入时若带 initialRunId，则定位/展开对应 run（状态被提升到单例，重挂载后保留）
  focusRun(props.initialRunId);
});

onUnmounted(() => {
  off('subagent:show-tasks', onShowTasks);
});
</script>
