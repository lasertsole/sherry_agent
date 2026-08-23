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
            v-if="runningTaskCount > 0"
            class="inline-flex items-center justify-center min-w-4 h-4 px-1 rounded-full text-[11px] leading-none text-white bg-red-500">
            {{ runningTaskCount }}
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
            v-else-if="taskRuns.length === 0"
            class="flex items-center justify-center h-full w-full text-[#868686]">
            {{ t('sidebar.noTasks') }}
          </div>
          <div
            v-else
            v-for="run in taskRuns"
            :key="run.run_id"
            class="rounded-lg border border-solid border-gray-light dark:border-gray-dark p-2.5 flex flex-col gap-1.5 cursor-pointer hover:border-theme-main transition-colors"
            @click="openFlowGraph(run)">
            <div class="flex items-center justify-between gap-2">
              <span
                class="flex-none inline-flex items-center gap-1 text-[11px] px-1.5 py-0.5 rounded-full leading-none"
                :class="badgeClass(run)">
                <i v-if="isRunning(run)" class="pi pi-spin pi-spinner text-[10px]" />
                {{ statusLabel(run) }}
              </span>
              <span class="flex-none text-[11px] text-[#868686]">{{ roleLabel(run) }}</span>
            </div>
            <div class="text-[13px] leading-snug line-clamp-2 break-words">{{ runLabel(run) }}</div>
            <div class="text-[11px] leading-snug text-[#868686] break-all">
              <span class="text-[#b0b0b0]">{{ t('sidebar.parentSession') }}: </span>{{ parentSessionLabel(run) }}
            </div>
            <template v-if="run.completion && run.completion.result_text">
              <div class="text-[11px] leading-snug text-[#868686] line-clamp-3 break-words border-t border-solid border-gray-100 dark:border-gray-700 pt-1">
                {{ run.completion.result_text }}
              </div>
            </template>
          </div>
        </div>
        <div
          v-if="taskRuns.length > 0"
          class="h-10 flex items-center justify-center text-[#868686]">
          <span class="text-[11px]">{{ t('sidebar.tasksUpdatedAt', { time: lastUpdatedText }) }}</span>
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
  clearCachedSessionMeta,
  cacheSubagentRuns,
  readCachedSubagentRuns,
  type CachedSubagentRun
} from '@/composables/db';
import { emit, on, off } from '@/composables/mitt';
import { getSessionList, clearSession, SESSION_ABORT_STREAM_EVENT } from '@/composables/messages';
import { fetchSubagentRuns, type SubagentRun } from '@/composables/bridge';
import { useSubagentWs } from '@/composables/ws';

const { t } = useI18n();
const router = useRouter();
const route = useRoute();
const localePath = useLocalePath();

/** 是否折叠（由父组件通过 v-model:collapsed 控制，折叠/展开按钮在父组件工具栏） */
const collapsed = defineModel<boolean>('collapsed', { default: false });

/** 当前会话 id（由父组件 v-model:current-session-id 双向同步，父组件用于加载角色快照） */
const currentSessionId = defineModel<string | undefined>('currentSessionId');

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
// 挂载后拉取会话列表 + 建立后台任务实时订阅（角色信息已由 ensureSessionCharacter 从本地 Dexie 加载）
onMounted(() => {
  loadSessionList();
  setupSubagentWs();
});

// 卸载时移除后台任务 Real-Time 订阅（WS 连接为模块级单例，交给后续组件复用，不在此关闭）
onUnmounted(() => {
  teardownSubagentSubscribe();
});

/* ------------------------------------------------------------------ */
/* 后台任务 Tab（子 Agent 运行记录）                                   */
/* ------------------------------------------------------------------ */
/** 侧边栏当前激活的标签页：'sessions'（会话）| 'tasks'（后台任务） */
const activeTab = ref<'sessions' | 'tasks'>('sessions');

/** 子 Agent 运行记录列表 */
const taskRuns = ref<SubagentRun[]>([]);
/** 后台任务加载中 */
const taskLoading = ref(false);
/** 上次成功拉取时间戳（毫秒） */
const lastTasksFetchedAt = ref<number>(0);
/** 是否曾接收过「后台任务」实时消息（避免首屏/重连时重复全量拉取） */
const subagentWsReady = ref(false);

/** 处于运行态的驻留子 Agent 数量（用于标签页红点角标）。 */
const runningTaskCount = computed(() => {
  return taskRuns.value.filter(run => isRunning(run)).length;
});

/** 切换标签页；切到「后台任务」时若尚无任何记录则首次加载（Dexie 先行 + 服务端补齐） */
const switchTab = (tab: 'sessions' | 'tasks') => {
  activeTab.value = tab;
  if (tab === 'tasks' && taskRuns.value.length === 0) {
    loadTaskRuns();
  }
};

/** 是否为运行中（RUNNING / INTERRUPTED 视为尚未结束） */
const isRunning = (run: SubagentRun): boolean => {
  const status = run?.execution?.status;
  return status === 'RUNNING' || status === 'INTERRUPTED';
};

/**
 * 将后端形状的 SubagentRun 规整为 Dexie 缓存形状 CachedSubagentRun。
 * 两者字段名一致，仅可空性/嵌套可选度不同，这里做一次性兜底，避免写入缓存时携带 undefined。
 */
const toCachedSubagentRun = (run: SubagentRun): CachedSubagentRun => ({
  run_id: run.run_id,
  child_session_key: run.child_session_key ?? null,
  requester_session_key: run.requester_session_key ?? null,
  task: run.task ?? null,
  task_name: run.task_name ?? null,
  label: run.label ?? null,
  spawn_mode: run.spawn_mode ?? null,
  context_mode: run.context_mode ?? null,
  agent_id: run.agent_id ?? null,
  depth: run.depth ?? null,
  role: run.role ?? null,
  control_scope: run.control_scope ?? null,
  generation: run.generation ?? null,
  swarm_group_id: run.swarm_group_id ?? null,
  swarm_run_state: run.swarm_run_state ?? null,
  ended_reason: run.ended_reason ?? null,
  pause_reason: run.pause_reason ?? null,
  execution: run.execution
    ? {
        status: run.execution.status ?? null,
        outcome: run.execution.outcome?.status ?? null,
        started_at: run.execution.started_at != null ? String(run.execution.started_at) : null,
        completed_at: run.execution.ended_at != null ? String(run.execution.ended_at) : null
      }
    : null,
  completion: run.completion
    ? {
        required: run.completion.required ?? null,
        owner_session_key: null,
        result_text: run.completion.result_text ?? null,
        captured_at: run.completion.captured_at != null ? String(run.completion.captured_at) : null
      }
    : null,
  delivery: run.delivery
    ? {
        status: run.delivery.status ?? null,
        attempt_count: run.delivery.attempt_count ?? null,
        delivered_at: run.delivery.delivered_at != null ? String(run.delivery.delivered_at) : null
      }
    : null
});

/** 从 Dexie 缓存形状还原为 UI 展示形状 SubagentRun。 */
const toSubagentRun = (c: CachedSubagentRun): SubagentRun => ({
  run_id: c.run_id,
  task_run_id: null,
  child_session_key: c.child_session_key ?? '',
  requester_session_key: c.requester_session_key ?? '',
  task: c.task ?? '',
  task_name: c.task_name ?? undefined,
  label: c.label ?? undefined,
  spawn_mode: c.spawn_mode ?? undefined,
  context_mode: c.context_mode ?? undefined,
  agent_id: c.agent_id ?? undefined,
  depth: c.depth ?? undefined,
  role: c.role ?? undefined,
  control_scope: c.control_scope ?? undefined,
  generation: c.generation ?? undefined,
  swarm_group_id: c.swarm_group_id ?? undefined,
  swarm_run_state: c.swarm_run_state ?? undefined,
  ended_reason: c.ended_reason ?? undefined,
  pause_reason: c.pause_reason ?? undefined,
  execution: {
    status: c.execution?.status ?? 'UNKNOWN',
    started_at: c.execution?.started_at != null ? Number(c.execution.started_at) : null,
    ended_at: c.execution?.completed_at != null ? Number(c.execution.completed_at) : null,
    outcome: c.execution?.outcome
      ? { status: c.execution.outcome, error: null }
      : { status: 'PENDING', error: null },
    transcript_target: undefined
  },
  completion: {
    required: c.completion?.required ?? false,
    result_text: c.completion?.result_text ?? null,
    captured_at: c.completion?.captured_at != null ? Number(c.completion.captured_at) : null
  },
  delivery: {
    status: c.delivery?.status ?? 'PENDING',
    payload: undefined,
    attempt_count: c.delivery?.attempt_count ?? 0,
    last_error: undefined,
    last_attempt_at: undefined,
    suspended_at: undefined,
    discard_reason: undefined,
    delivered_at: c.delivery?.delivered_at != null ? Number(c.delivery.delivered_at) : undefined
  }
});

/** 仅保留缓存中属于当前会话（或其后代）的运行记录。 */
const filterBySession = (runs: CachedSubagentRun[], sid: string): SubagentRun[] =>
  runs
    .filter(
      c =>
        c.requester_session_key === sid ||
        c.child_session_key === sid ||
        c.requester_session_key === activeSessionId.value
    )
    .map(toSubagentRun);

/** 刷新 taskRuns：先立即回显本地缓存（离线可用），再异步拉取后端补齐间隙。 */
const loadTaskRuns = async () => {
  const sid = activeSessionId.value;
  if (!sid) return;
  taskLoading.value = true;
  // 1) 本地缓存先行：读 IndexedDB 立即渲染，保证刷新/首屏不空窗
  try {
    const cached = await readCachedSubagentRuns();
    taskRuns.value = filterBySession(cached, sid);
  } catch (e) {
    console.warn('[SessionSidebar] 读取本地子任务缓存失败，回退服务端：', e);
  }
  // 2) 服务端间隙补齐：拉取整棵运行树并写入 Dexie，弥补 WS 断线期间丢失的事件
  try {
    const runs = await fetchSubagentRuns(sid, 'descendants');
    await cacheSubagentRuns(runs.map(toCachedSubagentRun));
    taskRuns.value = filterBySession(await readCachedSubagentRuns(), sid);
    lastTasksFetchedAt.value = Date.now();
  } catch (e) {
    // 网络失败：保留 Dexie 缓存兜底，不把列表清空，避免首屏抖动
    console.error('[SessionSidebar] 拉取子 Agent 运行记录失败（以本地缓存兜底）', e);
  } finally {
    taskLoading.value = false;
  }
};

/** 从 Dexie 缓存重建列表（WS 事件 / 切会话 / 重连后的本地即时更新）。 */
const refreshFromCache = async (sid?: string) => {
  const target = sid ?? activeSessionId.value;
  if (!target) return;
  try {
    const cached = await readCachedSubagentRuns();
    taskRuns.value = filterBySession(cached, target);
  } catch {
    // 缓存读取失败忽略，交由下一次 loadTaskRuns 兜底
  }
};

/** 建立 /subagents/ws 连接并订阅实时事件，使后台任务列表增量、实时。 */
const setupSubagentWs = () => {
  useSubagentWs({
    onReconnect: () => {
      // 重连成功后服务端会补发 ready，届时再拉一次全量补齐
      subagentWsReady.value = false;
    }
  });

  on('ws:subagent_spawned', (payload: unknown) => {
    const run = payload as SubagentRun;
    if (!run?.run_id) return;
    void cacheSubagentRuns([toCachedSubagentRun(run)]).then(() => refreshFromCache());
  });

  on('ws:subagent_ended', (payload: unknown) => {
    const run = payload as SubagentRun;
    if (!run?.run_id) return;
    // 结束后覆盖写回完整状态（含 outcome / delivery），供展示最终结果
    void cacheSubagentRuns([toCachedSubagentRun(run)]).then(() => refreshFromCache());
  });

  // ready：服务端已就绪，象征性地触发一次全量补齐（弥补连接建立前漏掉的事件）
  on('ws:subagents:ready', () => {
    subagentWsReady.value = true;
    if (activeTab.value === 'tasks') loadTaskRuns();
  });
};

/** 组件卸载前移除 mitt 订阅并把 WS 连接交给全局单例续命（无需关闭）。 */
const teardownSubagentSubscribe = () => {
  off('ws:subagent_spawned');
  off('ws:subagent_ended');
  off('ws:subagents:ready');
};

/** 运行记录的状态徽章样式（按 ExecutionStatus / RunOutcomeStatus 上色） */
const badgeClass = (run: SubagentRun): string => {
  const exec = run?.execution?.status;
  if (exec === 'RUNNING') return 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300';
  if (exec === 'INTERRUPTED') return 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300';
  const outcome = run?.execution?.outcome?.status;
  if (outcome === 'OK') return 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300';
  if (outcome === 'ERROR') return 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300';
  if (outcome === 'TIMEOUT' || outcome === 'KILLED')
    return 'bg-orange-100 text-orange-700 dark:bg-orange-900/40 dark:text-orange-300';
  return 'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-300';
};

/** 状态文案（优先运行态，其次配送态，最后结果态） */
const statusLabel = (run: SubagentRun): string => {
  const exec = run?.execution?.status;
  if (exec === 'RUNNING') return t('sidebar.statusRunning');
  if (exec === 'INTERRUPTED') return t('sidebar.statusInterrupted');
  const delivery = run?.delivery?.status;
  if (delivery === 'PENDING') return t('sidebar.statusPending');
  if (delivery === 'IN_PROGRESS') return t('sidebar.statusInProgress');
  if (delivery === 'DELIVERED') return t('sidebar.statusDelivered');
  const outcome = run?.execution?.outcome?.status;
  if (outcome === 'OK') return t('sidebar.statusDone');
  if (outcome === 'ERROR') return t('sidebar.statusError');
  if (outcome === 'TIMEOUT') return t('sidebar.statusTimeout');
  if (outcome === 'KILLED') return t('sidebar.statusKilled');
  return t('sidebar.statusUnknown');
};

/** 角色标签：root / 直属子任务 等 */
const roleLabel = (run: SubagentRun): string => {
  const depth = run?.depth ?? 0;
  if (depth <= 0) return t('sidebar.roleRoot');
  return `${t('sidebar.roleChild')}#${depth}`;
};

/** 运行条目主标题：优先 label/task_name，其次 task 文本 */
const runLabel = (run: SubagentRun): string => {
  return run?.label || run?.task_name || run?.task || run?.run_id || '-';
};

/** 调用方会话：展示发起该子任务的父 session_id（requester_session_key） */
const parentSessionLabel = (run: SubagentRun): string => {
  return run?.requester_session_key || '-';
};

/** 点击任务项：通知父组件打开右侧子 Agent 实时流程图面板，并携带该 run 的 id 用于高亮/重根 */
const openFlowGraph = (run: SubagentRun) => {
  emit('subagent:open-flow', run.run_id);
};

/** 上次更新时间文案（秒级） */
const lastUpdatedText = computed(() => {
  if (!lastTasksFetchedAt.value) return '';
  const sec = Math.max(0, Math.floor((Date.now() - lastTasksFetchedAt.value) / 1000));
  return t('sidebar.tasksAgoSeconds', { sec });
});

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
