<template>
  <!-- 左侧-历史记录区域 -->
  <!-- 移动端：固定定位，默认隐藏，通过按钮切换 -->
  <!-- md：固定定位，显示宽度 280px -->
  <!-- lg：相对定位，显示宽度 360px -->
  <div
    :class="[
      'relative h-full overflow-hidden transition-all duration-300',
      collapsed
        ? 'w-0 border-r-0'
        : 'w-[280px] md:w-[280px] lg:w-[360px] border-r border-solid border-gray-light bg-transparent dark:border-gray-dark dark:bg-transparent'
    ]">
    <!-- 内容固定宽度：折叠时由外层 overflow-hidden 整体裁切，内部元素不会被挤压换行 -->
    <div class="flex flex-col px-4 h-full w-[280px] md:w-[280px] lg:w-[360px]">
      <!-- LOGO区域 -->
      <div class="flex items-center h-15 text-xl">🍊{{ t('chatBox.defaultAiName') }}</div>
      <!-- 标签页切换：会话 / 后台任务 -->
      <div class="flex gap-1 my-3 rounded-lg p-1 bg-gray-100 dark:bg-gray-800">
        <button
          class="flex-1 h-8 rounded-md text-sm transition-all cursor-pointer"
          :class="
            activeTab === 'sessions'
              ? 'bg-white dark:bg-gray-700 text-primary font-medium shadow-sm'
              : 'text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200'
          "
          @click="switchTab('sessions')">
          {{ t('sidebar.tabSessions') }}
        </button>
        <button
          class="flex-1 h-8 rounded-md text-sm transition-all cursor-pointer flex items-center justify-center gap-1"
          :class="
            activeTab === 'tasks'
              ? 'bg-white dark:bg-gray-700 text-primary font-medium shadow-sm'
              : 'text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200'
          "
          @click="switchTab('tasks')">
          {{ t('sidebar.tabTasks') }}
          <span
            v-if="allRunningTaskCount > 0"
            class="inline-flex items-center justify-center min-w-4 h-4 px-1 rounded-full text-[11px] leading-none text-white bg-red-500">
            {{ allRunningTaskCount }}
          </span>
        </button>
      </div>

      <!-- ===== 会话 Tab ===== -->
      <template v-if="activeTab === 'sessions'">
        <!-- 新建对话 -->
        <Button
          icon="pi pi-comment"
          :label="t('toolbar.newChat')"
          class="mb-3"
          @click="handleCreateSession"
          size="small" />
        <!-- 记录列表 -->
        <div class="flex flex-col overflow-auto flex-1 gap-3">
          <div
            v-if="historyList.length === 0"
            class="flex items-center justify-center h-full w-full text-[#868686]">
            {{ t('history.noSessions') }}
          </div>
          <HistoryItem
            v-for="(item, index) in historyList"
            :key="item.id"
            :history-record="item"
            :is-active="currentSessionId === item.id"
            @choose-session="handleToggleSession"
            @delete-session="handleDeleteSession"
            v-model:selectedList="selectedSessionIds" />
        </div>
        <div class="h-17 flex items-center justify-between">
          <div class="flex items-center justify-center gap-1">
            <Checkbox
              :model-value="isCheckAllSession"
              :indeterminate="isIndeterminate"
              binary
              @update:model-value="handleToggleSelectAll" />
            <span>{{ t('history.selectAll') }}</span>
          </div>
          <Button
            icon="pi pi-trash"
            :label="t('history.batchDelete')"
            :disabled="selectedSessionIds.length === 0"
            @click="handleBatchDelete" />
        </div>
      </template>

      <!-- ===== 后台任务 Tab ===== -->
      <template v-else>
        <div class="flex flex-col overflow-auto flex-1 gap-2">
          <div
            v-if="taskLoading"
            class="flex items-center justify-center h-full w-full text-[#868686]">
            <i class="pi pi-spin pi-spinner mr-2" />{{ t('sidebar.tasksLoading') }}
          </div>
          <div
            v-else-if="rootTaskRuns.length === 0"
            class="flex items-center justify-center h-full w-full text-[#868686]">
            {{ t('sidebar.noTasks') }}
          </div>
          <template
            v-else
            v-for="group in groupedRootTaskRuns"
            :key="group.sessionId">
            <div
              class="flex items-center gap-2 pt-1.5 pb-0.5 text-[11px] font-semibold uppercase tracking-wide text-[#868686]">
              <span class="flex-none text-[#b0b0b0]">{{ t('sidebar.callingSession') }}:</span>
              <span class="truncate break-all">{{ group.sessionId }}</span>
              <span class="ml-auto flex-none text-[#868686]">({{ group.runs.length }})</span>
            </div>
            <div
              v-for="run in group.runs"
              :key="run.run_id"
              class="p-3 border border-solid rounded-lg text-[#ccc] cursor-pointer border-gray-light text-theme-main bg-white dark:bg-[#2a2a36]/[0.6] dark:border-[#555] flex flex-col gap-1.5 md:hover:bg-[#e4efff] md:dark:hover:bg-[#c1d6e5]"
              :class="{ 'text-theme-main bg-[#c1d6e5]!': focusedRunId === run.run_id }"
              @click="showTasksView(run)">
              <div class="flex items-center gap-2">
                <Checkbox
                  :model-value="selectedRunIds.has(run.run_id)"
                  binary
                  class="flex-none"
                  @update:model-value="handleToggleTask(run.run_id)"
                  @click.stop />
                <span
                  v-if="statusLabel(run) !== t('sidebar.statusUnknown')"
                  class="ml-auto flex-none inline-flex items-center gap-1 text-[11px] px-1.5 py-0.5 rounded-full leading-none"
                  :class="badgeClass(run)">
                  <i v-if="isRunning(run)" class="pi pi-spin pi-spinner text-[10px]" />
                  {{ statusLabel(run) }}
                </span>
              </div>
              <div class="text-[13px] leading-snug line-clamp-2 break-words">{{ run.label || run.task_name || '-' }}</div>
              <div class="text-[11px] leading-snug text-[#868686] break-all">
                <span class="text-[#b0b0b0]">{{ t('sidebar.startTime') }}: </span>{{ formatTime(run.execution.started_at) }}
                <span class="mx-1.5 text-[#b0b0b0]">/</span>
                <span class="text-[#b0b0b0]">{{ t('sidebar.endTime') }}: </span>{{ formatTime(run.execution.ended_at) }}
              </div>
            </div>
          </template>
        </div>
        <div
          v-if="rootTaskRuns.length > 0"
          class="h-17 flex items-center justify-between">
          <div class="flex items-center justify-center gap-1">
            <Checkbox
              :model-value="allSelected"
              :indeterminate="someSelected"
              binary
              @update:model-value="toggleSelectAllTasks()" />
            <span>{{ t('sidebar.tasksSelectAll') }}</span>
          </div>
          <Button
            icon="pi pi-trash"
            :label="t('sidebar.tasksBatchDelete')"
            :disabled="selectedRunIds.size === 0 || deletingRunIds.size > 0"
            :loading="deletingRunIds.size > 0"
            @click="handleBatchDeleteTasks" />
        </div>
      </template>
    </div>
  </div>
</template>

<script lang="ts">
// 方法/类型（普通 script 块：仅用于向父组件导出 ensureSessionCharacter，供其复用）
import type { CachedCharacter } from '@/composables/db';
import {
  GLOBAL_SESSION_KEY,
  DEFAULT_CACHED_CHARACTER,
  cacheCharacter,
  readCachedCharacter
} from '@/composables/db';

/**
 * 默认角色显示信息（内置：远野汉娜 / 橘雪莉 + 默认头像 URL，见 `defaultCharacter.ts`）。
 * 用于在会话尚未锁定角色快照时，作为 Dexie 锁定的兜底数据源。
 */
const defaultCharacter = (): { userName: string; userAvatar: string; aiName: string; aiAvatar: string } => ({
  userName: DEFAULT_CACHED_CHARACTER.userName,
  userAvatar: DEFAULT_CACHED_CHARACTER.userAvatar,
  aiName: DEFAULT_CACHED_CHARACTER.aiName,
  aiAvatar: DEFAULT_CACHED_CHARACTER.aiAvatar
});

/**
 * 确保指定会话已锁定自己的角色快照。
 *
 * 命名逻辑：系统配置-角色配置编辑的是「全局待定 profile」（`GLOBAL_SESSION_KEY` 行）。
 * 每个会话在首次打开时，把当时的全局 profile 拷贝并锁定到自己的 `session_id` 行；
 * 之后全局更新（改头像/名字）不再作用于已锁定快照的旧会话，仅新会话会取到最新全局值。
 * 锁定结果由 [sid].vue 通过 `readCachedCharacter(sessionId)` 消费。
 *
 * 导出供 home/index.vue 复用（系统配置保存后加载当前会话快照、首屏 default 会话初始化）。
 *
 * @param sessionId 会话 ID
 */
export async function ensureSessionCharacter(sessionId: string) {
  try {
    const [globalSnap, sessionSnap] = await Promise.all([
      readCachedCharacter(GLOBAL_SESSION_KEY),
      readCachedCharacter(sessionId)
    ]);
    // 会话已有快照（旧会话锁定的头像/名字）→ 保持现状，不覆盖旧会话快照。
    if (sessionSnap) {
      return;
    }
    // 会话尚无快照（新建或从未打开过的会话）→ 用全局 profile 快照并锁定。
    // 注意：`base` 可能是全局行（含 session_id=GLOBAL_SESSION_KEY），
    // 必须用 `...base` 之后显式覆盖 session_id，避免把真实会话的 key 写进全局行。
    const base = globalSnap ?? defaultCharacter();
    const locked: CachedCharacter = { ...base, session_id: sessionId };
    await cacheCharacter(locked);
  } catch (error) {
    // Dexie 读写异常时不阻塞聊天。
    console.warn('[ensureSessionCharacter] 读取角色快照失败：', error);
  }
}
</script>

<script setup lang="ts">
// components
import HistoryItem from './HistoryItem.vue';
// function
import { computed, onMounted, onUnmounted, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import type { SessionRecord } from '../type.ts';
import {
  clearCachedCharacter,
  cacheSessionMeta,
  readCachedSessionMetaList,
  clearCachedSessionMeta
} from '@/composables/db';
import { emit, on, off } from '@/composables/mitt';
import { getSessionList, clearSession, SESSION_ABORT_STREAM_EVENT } from '@/composables/messages';
import type { SubagentRun } from '@/composables/bridge';
import { useSubagentTasks } from '@/composables/useSubagentTasks';
import dayjs from 'dayjs';

const { t } = useI18n();
const router = useRouter();
const route = useRoute();
const localePath = useLocalePath();

// 后台任务共享状态（模块级单例，与右侧完整任务列表页共用同一份响应式数据）
const {
  taskRuns,
  allTaskRuns,
  rootTaskRuns,
  groupedRootTaskRuns,
  taskLoading,
  runningTaskCount,
  allRunningTaskCount,
  lastUpdatedText,
  selectedRunIds,
  deletingRunIds,
  allSelected,
  someSelected,
  isRunning,
  badgeClass,
  statusLabel,
  parentSessionLabel,
  initTasks,
  setTasksTabActive,
  focusRun,
  focusedRunId,
  loadTaskRuns,
  toggleTaskSelection,
  toggleSelectAllTasks,
  deleteSelectedTasks
} = useSubagentTasks();

/** 是否折叠（由父组件通过 v-model:collapsed 控制，折叠/展开按钮在父组件工具栏） */
const collapsed = defineModel<boolean>('collapsed', { default: false });

/** 当前会话 id（由父组件 v-model:current-session-id 双向同步，父组件用于加载角色快照） */
const currentSessionId = defineModel<string | undefined>('currentSessionId');

/** 渲染执行时间：epoch 毫秒 → 本地可读字符串；空值/非法值显示占位符 '-' */
function formatTime(ms: number | null | undefined): string {
  if (ms == null || Number.isNaN(Number(ms))) return '-';
  return dayjs(Number(ms)).format('YYYY-MM-DD HH:mm:ss');
}

/** 历史会话 */
const historyList = ref<SessionRecord[]>([]);

/** 全选状态 */
const isCheckAllSession = ref<boolean>(false);
/** 选择的会话 */
const selectedSessionIds = ref<string[]>([]);
/** 会话选择状态 */
const isIndeterminate = computed(() => {
  if (selectedSessionIds.value.length > 0 && selectedSessionIds.value.length < historyList.value.length) {
    return true;
  } else {
    return false;
  }
});
/** 监听选择 */
watch(
  () => selectedSessionIds.value,
  newVal => {
    if (newVal.length === historyList.value.length) {
      isCheckAllSession.value = true;
    } else {
      isCheckAllSession.value = false;
    }
  }
);

/**
 * 从服务端拉取全部会话列表并填充左侧历史列表。
 *
 * 会话列表的唯一权威来源是服务端（context_engine）。这里会把服务端返回的
 * `{session_id, last_time, title}` 映射为前端 `SessionRecord`（id / createTime / title）。
 * 本地新建但尚未持久化的会话（createTime 为本地时间）会保留在列表头部。
 */
const loadSessionList = async () => {
  try {
    const sessions = await getSessionList();
    // 合并本地新建但服务端尚不存在的会话（IndexedDB 占位，刷新后仍能恢复）：
    // 1) 读取 IndexedDB 中持久化的占位会话（新建空会话尚未发消息）；
    // 2) 服务端已有记录（发过消息）的会话直接从内存列表保留，并顺手清除其占位；
    // 3) 内存 `historyList` 中的本地项（本次会话新建但尚未写入 IndexedDB，兜底）。
    let localPlaceholders = historyList.value.filter(s => !sessions.some(row => row.id === s.id));
    const serverIds = new Set(sessions.map(row => row.id));
    // 服务端已有记录的会话，删除其本地占位（已晋升为真实服务端会话）。
    const placeholders = await readCachedSessionMetaList();
    for (const p of placeholders) {
      if (serverIds.has(p.id)) {
        clearCachedSessionMeta(p.id);
      }
    }
    // 合并：IndexedDB 占位（刷新恢复） + 内存本地项（本次会话兜底），去重。
    const localById = new Map<string, SessionRecord>();
    for (const p of placeholders) {
      localById.set(p.id, { id: p.id, title: p.title, createTime: p.createTime });
    }
    for (const s of localPlaceholders) {
      if (!localById.has(s.id)) localById.set(s.id, s);
    }
    localPlaceholders = Array.from(localById.values());
    // 占位会话按最新优先（createTime 倒序，字符串格式 YYYY-MM-DD HH:mm 可字典序比较）。
    localPlaceholders.sort((a, b) => (b.createTime < a.createTime ? -1 : 1));
    historyList.value = [...localPlaceholders, ...sessions];
  } catch (error) {
    // 服务端不可达时：本次会话内存态保留，并尝试从 IndexedDB 恢复已持久化的占位会话
    console.warn('[loadSessionList] 拉取会话列表失败：', error);
    try {
      const placeholders = await readCachedSessionMetaList();
      const localById = new Map<string, SessionRecord>();
      for (const p of placeholders) {
        localById.set(p.id, { id: p.id, title: p.title, createTime: p.createTime });
      }
      for (const s of historyList.value) {
        if (!localById.has(s.id)) localById.set(s.id, s);
      }
      historyList.value = Array.from(localById.values());
    } catch (cacheErr) {
      console.warn('[loadSessionList] 恢复本地占位会话失败：', cacheErr);
    }
  }
};

/** 新增会话：生成随机 session_id，加入列表并路由到新会话页（KeepAlive 按 sid 缓存） */
const handleCreateSession = () => {
  const sessionId = crypto.randomUUID();
  const now = new Date();
  const pad = (n: number) => String(n).padStart(2, '0');
  const createTime = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} ${pad(now.getHours())}:${pad(now.getMinutes())}`;
  const newSession: SessionRecord = {
    id: sessionId,
    title: t('history.newSession'),
    createTime
  };
  historyList.value = [newSession, ...historyList.value];
  currentSessionId.value = sessionId;
  // 新会话：立即用当前全局 profile 创建并锁定角色快照，保证头像/名字正确显示
  ensureSessionCharacter(sessionId);
  // 切回「会话」展示态：通知右侧 [sid].vue 恢复聊天区
  emit('subagent:show-chat');
  setTasksTabActive(false);
  // 持久化占位会话（新建即写入 IndexedDB），保证刷新/重开后该空会话仍保留在列表
  // （服务端会话列表由消息表派生，未发消息前无记录，只能靠本地占位恢复）。
  cacheSessionMeta({ id: sessionId, title: t('history.newSession'), createTime, updatedAt: Date.now() });
  router.push(localePath(`/home/${sessionId}`));
};

/**
 * 会话切换：路由到对应会话页。
 * [sid].vue 由 KeepAlive 按 session_id 缓存，切换时原样恢复其草稿/滚动/流式状态。
 */
const handleToggleSession = (id: string) => {
  if (currentSessionId.value === id) return;
  currentSessionId.value = id;
  // 切换会话：加载该会话已锁定的角色快照（无快照则用全局 profile 锁定）
  ensureSessionCharacter(id);
  // 切回「会话」展示态：通知右侧 [sid].vue 恢复聊天区
  emit('subagent:show-chat');
  setTasksTabActive(false);
  router.push(localePath(`/home/${id}`));
};

/**
 * 删除会话：调用服务端 clearSession，成功后从列表移除。
 * 若删除的是当前激活会话，则路由回首页空态（[sid].vue 实例由 KeepAlive 释放）。
 */
const handleDeleteSession = async (id: string) => {
  try {
    const ok = await clearSession(id);
    if (!ok) {
      console.warn('[handleDeleteSession] 删除会话失败，保留列表项：', id);
      return;
    }
    historyList.value = historyList.value.filter(s => s.id !== id);
    selectedSessionIds.value = selectedSessionIds.value.filter(sid => sid !== id);
    // 同步清理该会话的角色快照缓存
    clearCachedCharacter(id);
    // 同步清理本地占位会话缓存（IndexedDB），避免删除后仍残留占位
    clearCachedSessionMeta(id);
    // 该会话可能仍在流式生成（尤其是非激活会话，其 [sid].vue 仍被 KeepAlive 缓存且流未中止）。
    // 广播中止事件，让对应的 [sid].vue 实例 abort 其 AbortController，避免删除后流仍在后台推块、污染聊天状态。
    emit(SESSION_ABORT_STREAM_EVENT, id);
    if (currentSessionId.value === id) {
      currentSessionId.value = undefined;
      router.push(localePath('/home'));
    }
  } catch (error) {
    console.warn('[handleDeleteSession] 删除会话异常，保留列表项：', id, error);
  }
};

/** 全选/取消全选：在「全选 / 部分选择 / 未选择」三种状态间切换 */
const handleToggleSelectAll = (checked: boolean) => {
  if (checked) {
    // 勾选全选：当前列表全部选中
    selectedSessionIds.value = historyList.value.map(s => s.id);
  } else {
    // 取消全选：清空当前选择
    selectedSessionIds.value = [];
  }
};

/**
 * 批量删除会话：逐个调用服务端 clearSession，成功后统一从列表移除。
 * 若其中有当前激活会话，则路由回首页空态。
 */
const handleBatchDelete = async () => {
  if (selectedSessionIds.value.length === 0) return;
  if (!window.confirm(t('history.batchDeleteConfirm'))) return;

  const ids = [...selectedSessionIds.value];
  const remain: string[] = [];
  let failed = false;
  for (const id of ids) {
    try {
      const ok = await clearSession(id);
      if (!ok) {
        failed = true;
        remain.push(id);
      }
    } catch (error) {
      failed = true;
      remain.push(id);
      console.warn('[handleBatchDelete] 删除会话异常：', id, error);
    }
  }

  const deleted = ids.filter(id => !remain.includes(id));
  if (deleted.length > 0) {
    historyList.value = historyList.value.filter(s => !deleted.includes(s.id));
    // 同步清理被删会话的角色快照缓存
    for (const id of deleted) clearCachedCharacter(id);
    // 被删会话可能仍在流式生成（KeepAlive 缓存内的非激活实例流未中止），
    // 逐个广播中止事件，让对应 [sid].vue 实例 abort 其 AbortController。
    for (const id of deleted) emit(SESSION_ABORT_STREAM_EVENT, id);
  }
  if (currentSessionId.value && deleted.includes(currentSessionId.value)) {
    currentSessionId.value = undefined;
    router.push(localePath('/home'));
  }
  selectedSessionIds.value = remain;

  if (failed && remain.length > 0) {
    console.warn('[handleBatchDelete] 部分会话删除失败，已保留：', remain);
  }
};

// 首屏加载 default 会话的角色显示信息（头像 + 名字）
ensureSessionCharacter('default');
// 挂载后拉取会话列表 + 初始化后台任务（WS 订阅为模块级单例，幂等；角色信息已由 ensureSessionCharacter 从本地 Dexie 加载）
// 收到「展示聊天」事件（新建会话/切换会话/后台任务「返回会话」）时，
// 切回「会话」标签，确保会话列表可见并高亮目标 session box。
const onShowChatSwitchTab = () => switchTab('sessions');
onMounted(() => {
  loadSessionList();
  initTasks(activeSessionId.value);
  on('subagent:show-chat', onShowChatSwitchTab);
});
onUnmounted(() => {
  off('subagent:show-chat', onShowChatSwitchTab);
});

/* ------------------------------------------------------------------ */
/* 后台任务 Tab（子 Agent 运行记录）                                   */
/* ------------------------------------------------------------------ */
/** 侧边栏当前激活的标签页：'sessions'（会话）| 'tasks'（后台任务） */
const activeTab = ref<'sessions' | 'tasks'>('sessions');

/**
 * 切换标签页（仅切换侧边栏左侧展示的列表 + 后台任务的加载态，**不**切换右侧视图）。
 * 右侧视图只在点击具体「会话 Box」（handleToggleSession / handleCreateSession）或
 * 「后台任务 Box」（showTasksView）时才随之切换。
 * - 切到「后台任务」：标记后台任务处于展示态，令 WS 就绪时拉取全量任务数据供列表展示。
 * - 切到「会话」：解除该标记。
 */
const switchTab = (tab: 'sessions' | 'tasks') => {
  activeTab.value = tab;
  if (tab === 'tasks') {
    setTasksTabActive(true);
    void loadTaskRuns();
  } else {
    setTasksTabActive(false);
  }
};

/**
 * 点击任务项：切换到「后台任务」展示态，并定位/展开/高亮该 run。
 * 有激活会话（route 带 sid）时，发出 subagent:show-tasks 事件，由 [sid].vue 内嵌视图接收并置为任务展示态；
 * 无激活会话（根路径 /home，[sid].vue 未挂载，事件无人接收）时，直接聚焦该 run（模块级单例状态跨路由保留）
 * 并导航到独立任务页 /home/tasks/{父会话}——该页始终挂载 SubagentTasksView，可从单例状态读到已聚焦的 run。
 */
const showTasksView = (run: SubagentRun) => {
  activeTab.value = 'tasks';
  // 记录当前聚焦/打开的 run，用于侧边栏任务 box 的激活态高亮（与会话列表项一致）
  focusRun(run.run_id);
  const sid = route.params.sid;
  if (typeof sid === 'string' && sid) {
    // 有激活会话：走内嵌视图事件流（由 [sid].vue 的 onShowTasks 把 viewMode 切为 'tasks'）
    emit('subagent:show-tasks', run.run_id);
    setTasksTabActive(true);
  } else {
    // 无激活会话：聚焦 + 导航到独立任务页（跨会话任务树的父会话）
    const parentSid = run.requester_session_key;
    router.push(localePath(`/home/tasks/${parentSid || 'default'}`));
  }
};

/**
 * 切换单个任务的选中态（仅由任务卡片内的复选框触发）。
 * 卡片本体点击改为 showTasksView（打开任务详情页），避免遮挡打开逻辑。
 */
const handleToggleTask = (runId: string) => {
  if (deletingRunIds.value.has(runId)) return;
  toggleTaskSelection(runId);
};

/** 批量删除当前选中的任务（各任务连同其整棵子树一并彻底清空前后端缓存）。 */
const handleBatchDeleteTasks = async () => {
  if (selectedRunIds.value.size === 0) return;
  if (!window.confirm(t('sidebar.tasksBatchDeleteConfirm'))) return;
  try {
    const removed = await deleteSelectedTasks();
    if (removed > 0) emit('subagent:refresh-tasks');
  } catch (error) {
    console.error('[SessionSidebar] 批量删除后台任务失败：', error);
  }
};

// 更强保障：以浏览器 URL 末尾的 session_id 作为激活态的「唯一事实来源」。
// 用 immediate 监听 route.params.sid，同时覆盖三种场景：
//   1) 刷新/直达 /home/{sid}：组件挂载时立即恢复高亮（此前 currentSessionId 初始为 undefined，
//      不恢复则侧边栏无任何激活态背景）；
//   2) 浏览器内导航（后退/前进/改 URL）：sid 变化时同步移动高亮，无需整页刷新；
//   3) 时序竞态：无论 loadSessionList 列表返回先后，只要 URL 带 sid，就始终以它为激活项。
const activeSessionId = computed(() => {
  const sid = route.params.sid;
  return typeof sid === 'string' && sid ? sid : undefined;
});
watch(
  activeSessionId,
  async sid => {
    currentSessionId.value = sid;
    if (sid) {
      // 加载该会话已锁定的角色快照（无快照则用全局 profile 锁定）
      await ensureSessionCharacter(sid);
    }
  },
  { immediate: true }
);

// 切换激活会话时刷新后台任务（仅当用户曾打开过该 Tab）
watch(
  activeSessionId,
  () => {
    if (activeTab.value === 'tasks') loadTaskRuns();
  },
  { immediate: false }
);
</script>
