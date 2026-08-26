/**
 * 共享「后台任务」状态 composable（模块级单例）
 *
 * 职责：集中管理子 Agent 运行记录（taskRuns）的全部状态与加载/WS/Dexie 缓存逻辑，
 * 供左侧侧边栏（SessionSidebar.vue）与右侧完整任务列表页（SubagentTasksView.vue）
 * 共用同一份响应式数据，保证两处实时一致、无重复订阅。
 *
 * 采用模块级单例（组合 Setup 外声明状态）而非实例级：无论被多少个组件调用，
 * 拿到的都是同一份 taskRuns / taskLoading / …，WS 订阅也只建立一次。
 */
import { computed, ref } from 'vue';
import { useI18n } from 'vue-i18n';
import { on, off } from './mitt';
import { fetchSubagentRuns, fetchSubagentRunSubtree, deleteSubagentRunSubtree, type SubagentRun } from './bridge';
import { cacheSubagentRuns, readCachedSubagentRuns, deleteCachedSubagentRuns, type CachedSubagentRun } from './db';
import { useSubagentWs } from './ws';

/* ------------------------------------------------------------------ */
/* 模块级单例状态（跨调用共享的唯一事实来源）                           */
/* ------------------------------------------------------------------ */
/** 子 Agent 运行记录列表（按当前会话过滤，供聊天页跳转栏/侧边栏红点判定本会话任务） */
const taskRuns = ref<SubagentRun[]>([]);
/** 全局子 Agent 运行记录列表（含所有会话，供后台任务视图展示全部 session 的任务） */
const allTaskRuns = ref<SubagentRun[]>([]);
/** 后台任务加载中 */
const taskLoading = ref(false);
/** 上次成功拉取时间戳（毫秒） */
const lastTasksFetchedAt = ref<number>(0);
/** 是否曾接收过「后台任务」实时消息（避免首屏/重连时重复全量拉取） */
const subagentWsReady = ref(false);
/** 上一次已初始化的 session（用于切换会话时刷新列表） */
let lastLoadedSessionId: string | undefined;

/** 后台任务 tab 当前选中的根 run_id 集合（多选/全选/批量删除用） */
const selectedRunIds = ref<Set<string>>(new Set());
/** 后台任务 tab 批量删除进行中 */
const deletingRunIds = ref<Set<string>>(new Set());

/** 当前「后台任务」是否处于展示态（侧边栏 tasks 或右侧任务视图激活时为 true） */
const tasksTabActive = ref(false);

/* 会话键前缀：后端把子任务归属到调用方会话时使用带前缀的 key，展示/Nav 需要归一化为 bare UUID */
const SESSION_KEY_PREFIXES = ['agent:main:session:', 'agent:subagent:'];

/** 归一化会话键：strip 前缀得到 bare UUID；无前缀的键（已为 bare/'/'-'/默认）原样返回。 */
function normalizeSessionKey(key: string | null | undefined): string | null {
  if (!key) return null;
  for (const prefix of SESSION_KEY_PREFIXES) {
    if (key.startsWith(prefix)) {
      const bare = key.slice(prefix.length);
      return bare || null;
    }
  }
  return key;
}

/** 当前「仍存在的会话」bare UUID 集合。
 *  数据源：服务端 `/sessions` 权威列表 + 本地 Dexie 会话占位；由 loadSubagentValidSessions() 填充。
 *  用途：SubagentTasksView 校验一条 run 的「返回会话」目标是否真实存在——不存在则隐藏该按钮。 */
const subagentValidSessionIds = ref<Set<string>>(new Set());
/** 是否已加载过（避免每次切换会话重复拉取会话列表）。 */
let subagentSessionsLoaded = false;

/**
 * 拉取并缓存「仍存在的会话」集合。
 * 供 SubagentTasksView 的「返回会话」存在性校验：requester_session_key 归一化后不在该集合中的
 * orphaned run，其任务 box 照常展示，但「返回会话」按钮被隐藏。
 * 幂等：只会真正拉取一次（除非跨窗口状态清空，可显式重新调用）。
 */
async function loadSubagentValidSessions(): Promise<void> {
  if (subagentSessionsLoaded) return;
  const { getSessionList } = await import('@/composables/messages');
  const { readCachedSessionMetaList } = await import('@/composables/db');
  try {
    const sessions = await getSessionList();
    const placeholders = await readCachedSessionMetaList();
    const set = new Set<string>();
    for (const s of sessions) if (s.id) set.add(s.id);
    for (const p of placeholders) if (p.id) set.add(p.id);
    subagentValidSessionIds.value = set;
    subagentSessionsLoaded = true;
  } catch (error) {
    // 拉取失败不阻断 UI：等价于「无法确认目标会话」，按钮按无有效目标隐藏即可。
    console.warn('[useSubagentTasks] 拉取会话列表失败，无法校验「返回会话」目标：', error);
  }
}

/* ------------------------------------------------------------------ */
/* 任务视图展开/流程图状态（提升到单例，跨 chat↔tasks 切换保活）        */
/* ------------------------------------------------------------------ */

/** 当前展开的 run_id（点击任务卡片头部切换展开/收起）；chat↔tasks 切换后保留 */
const expandedRunId = ref<string | undefined>(undefined);
/** 内嵌流程图选中 run（透传给 SubagentFlowGraph 的 defineModel selected-run-id） */
const selectedRunId = ref<string | undefined>(undefined);
/** 右侧任务视图当前聚焦的 run_id（点击左侧后台任务 Box 时设置，用于展示该 run 的子树） */
const focusedRunId = ref<string | undefined>(undefined);

/** 点击任务卡片头部：展开/收起指定 run，并同步选中态。 */
function toggleExpandRun(runId: string): void {
  if (expandedRunId.value === runId) {
    expandedRunId.value = undefined;
    selectedRunId.value = undefined;
  } else {
    expandedRunId.value = runId;
    selectedRunId.value = runId;
  }
}

/** 定位/展开指定 run（点击侧边栏任务项时调用），若有则展开并同步选中态。 */
function focusRun(runId: string | undefined): void {
  if (!runId) return;
  expandedRunId.value = runId;
  selectedRunId.value = runId;
  // 同步记录右侧视图聚焦的 run，用于按该 run 展示其子树
  focusedRunId.value = runId;
}

/** 从任务视图切换回聊天时，保留展开态但不重置（如需清空可显式调用）。 */
function resetFlowState(): void {
  expandedRunId.value = undefined;
  selectedRunId.value = undefined;
}

/** 是否已建立过 WS 订阅（单例守护，避免重复 on 订阅） */
let subscribed = false;

/* ------------------------------------------------------------------ */
/* 内部工具函数                                                         */
/* ------------------------------------------------------------------ */

/** 是否为运行中（RUNNING / INTERRUPTED 视为尚未结束） */
function isRunning(run: SubagentRun): boolean {
  const status = run?.execution?.status;
  return status === 'RUNNING' || status === 'INTERRUPTED';
}

/**
 * 将后端形状的 SubagentRun 规整为 Dexie 缓存形状 CachedSubagentRun。
 * 两者字段名一致，仅可空性/嵌套可选度不同，这里做一次性兜底，避免写入缓存时携带 undefined。
 */
function toCachedSubagentRun(run: SubagentRun): CachedSubagentRun {
  return {
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
  };
}

/** 从 Dexie 缓存形状还原为 UI 展示形状 SubagentRun。 */
function toSubagentRun(c: CachedSubagentRun): SubagentRun {
  return {
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
  };
}

/* ------------------------------------------------------------------ */
/* 加载 / 刷新逻辑                                                      */
/* ------------------------------------------------------------------ */

/**
 * 解析当前激活会话 id。
 * 兼容两种来源：优先从 URL pathname 提取（module 级安全，不依赖 setup 上下文），
 * 也可由调用方通过参数显式传入（推荐，来源与侧边栏/路由保持一致）。
 */
function resolveSid(force?: string): string | undefined {
  if (force) return force;
  if (typeof window === 'undefined') return undefined;
  const segs = window.location.pathname.split('/').filter(Boolean);
  const sid = segs[segs.length - 1];
  return sid && sid !== 'home' ? sid : undefined;
}

/** 仅保留缓存中属于当前会话（或其子任务）的运行记录。 */
function filterBySession(runs: CachedSubagentRun[], sid: string | undefined): SubagentRun[] {
  if (!sid) return [];
  return runs
    .filter(c => c.requester_session_key === sid || c.child_session_key === sid)
    .map(toSubagentRun);
}

/** 从 Dexie 缓存重建列表（WS 事件 / 切会话 / 重连后的本地即时更新）。 */
async function refreshFromCache(sid?: string): Promise<void> {
  try {
    const cached = await readCachedSubagentRuns();
    // 全局缓存是「所有会话」的累计数据（Dexie 表为全局表），直接映射为全局任务视图数据
    allTaskRuns.value = cached.map(toSubagentRun);
    // 会话过滤视图：供聊天页跳转栏/侧边栏红点判定本会话任务
    const target = resolveSid(sid);
    taskRuns.value = target ? filterBySession(cached, target) : [];
  } catch {
    // 缓存读取失败忽略，交由下一次 loadTaskRuns 兜底
  }
}

/** 刷新 taskRuns：先立即回显本地缓存（离线可用），再异步拉取后端补齐间隙。 */
async function loadTaskRuns(sid?: string): Promise<void> {
  const target = resolveSid(sid);
  taskLoading.value = true;
  // 1) 本地缓存先行：读 IndexedDB 立即渲染，保证刷新/首屏不空窗
  try {
    const cached = await readCachedSubagentRuns();
    if (target) taskRuns.value = filterBySession(cached, target);
    else taskRuns.value = [];
    // 全局视图同步：无论是否有 target，全局任务列表始终取自全量缓存
    allTaskRuns.value = cached.map(toSubagentRun);
  } catch (e) {
    console.warn('[useSubagentTasks] 读取本地子任务缓存失败，回退服务端：', e);
  }
  // 2) 服务端间隙补齐：拉取整棵运行树并写入 Dexie，弥补 WS 断线期间丢失的事件
  if (target) {
    try {
      const runs = await fetchSubagentRuns(target, 'descendants');
      await cacheSubagentRuns(runs.map(toCachedSubagentRun));
      const cached = await readCachedSubagentRuns();
      taskRuns.value = filterBySession(cached, target);
      // 拉取后全量缓存已补齐，同步刷新全局任务列表
      allTaskRuns.value = cached.map(toSubagentRun);
      lastTasksFetchedAt.value = Date.now();
    } catch (e) {
      // 网络失败：保留 Dexie 缓存兜底，不把列表清空，避免首屏抖动
      console.error('[useSubagentTasks] 拉取子 Agent 运行记录失败（以本地缓存兜底）', e);
    }
  }
  taskLoading.value = false;
}

/* ------------------------------------------------------------------ */
/* 实时订阅（单例）                                                     */
/* ------------------------------------------------------------------ */

/**
 * 建立 /subagents/ws 连接并订阅实时事件，使后台任务列表增量、实时。
 * 通过 subscribed 守护确保无论多少组件调用，订阅只建立一次。
 */
function setupSubagentWs(): void {
  if (subscribed) return;
  subscribed = true;

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
    // 仅在「后台任务」处于展示态时触发全量补齐（实际查看时拉取，避免无谓请求）
    if (tasksTabActive.value) void loadTaskRuns();
  });
}

/* ------------------------------------------------------------------ */
/* 对外状态                                                             */
/* ------------------------------------------------------------------ */

/** 处于运行态的驻留子 Agent 数量（用于「会话」标签页红点角标，当前会话过滤）。 */
const runningTaskCount = computed(() => taskRuns.value.filter(run => isRunning(run)).length);

/** 处于运行态的全部子 Agent 数量（用于「后台任务」标签页红点角标，跨所有会话）。 */
const allRunningTaskCount = computed(() => allTaskRuns.value.filter(run => isRunning(run)).length);

/** 后台任务 tab 展示列表：仅「第一层直属任务」（depth === 1，即各会话直接派生的子任务），跨所有会话。
 *  orphaned run（调用方会话已被销毁的 stale 缓存）**仍展示**；其「返回会话」由
 *  SubagentTasksView 基于 wouldExistSession 校验，不存在时隐藏按钮（但任务 box 保留）。 */
const rootTaskRuns = computed(() =>
  allTaskRuns.value.filter(run => run?.depth === 1),
);

/** 定义「按调用方会话聚类」后的后台任务分组结构：每组一个 calling session_id + 该 group 下的第一层任务。 */
export interface TaskSessionGroup {
  /** 调用方 session_id（requester_session_key；为空时归入 fallback 键 '-'） */
  sessionId: string;
  /** 该调用方会话下派生的第一层任务列表（保持 rootTaskRuns 原顺序） */
  runs: SubagentRun[];
}

/**
 * 后台任务 tab 聚类列表：按「调用方 session_id」（rootTaskRuns 里每个根任务的
 * requester_session_key）把 task box 分组。同一调用方会话派生的多个根任务归入同一组，
 * 便于左栏按 session 聚类直观展示；组顺序按 sessionId 稳定排序（空值组排最后）。
 */
const groupedRootTaskRuns = computed<TaskSessionGroup[]>(() => {
  const groups = new Map<string, SubagentRun[]>();
  for (const run of rootTaskRuns.value) {
    const key = run.requester_session_key || '-';
    const list = groups.get(key);
    if (list) list.push(run);
    else groups.set(key, [run]);
  }
  return Array.from(groups.entries())
    .sort(([a], [b]) => {
      // 空值组排最后；其余按 sessionId 字典序
      if (a === '-') return 1;
      if (b === '-') return -1;
      return a < b ? -1 : a > b ? 1 : 0;
    })
    .map(([sessionId, runs]) => ({ sessionId, runs }));
});

/**
 * 右侧任务视图展示列表：点击后台任务 box 时，显示该 box 所属「调用 session」
 * （其顶层 depth-1 祖先任务的 requester_session_key）下的**所有第一层根任务**，
 * 并各自展开其整棵后代子树。未聚焦时回退为展示全部第一层任务。
 *
 * 父子关联方式（SubagentRun 无 parent_run_id）：父 run 的 child_session_key = K，
 * 则所有 requester_session_key === K 的 run 都是其直接子任务。由此：
 * - 向上：run.requester_session_key === 某父 run 的 child_session_key，则找到其祖先；
 * - 向下：从根按 child_session_key → requester_session_key 链路逐层扩散收集后代。
 */
const focusedSubtreeRuns = computed<SubagentRun[]>(() => {
  const rootId = focusedRunId.value;
  if (!rootId) return rootTaskRuns.value;
  const pool = allTaskRuns.value;

  // 按 requester_session_key 预建「直接子任务」索引，供向上/向下双向检索
  const byRequester = new Map<string, SubagentRun[]>();
  for (const run of pool) {
    const key = run.requester_session_key;
    if (!key) continue;
    const list = byRequester.get(key);
    if (list) list.push(run);
    else byRequester.set(key, [run]);
  }
  // 按 child_session_key → 指向「由其派生的父 run」的索引，供向上追溯祖先
  const parentByChildSession = new Map<string, SubagentRun>();
  for (const run of pool) {
    if (run.child_session_key) parentByChildSession.set(run.child_session_key, run);
  }

  // 从聚焦 run 出发向上追溯至最顶层的 depth-1 祖先根任务
  let top = pool.find(r => r.run_id === rootId);
  if (!top) return [];
  let guard = 0;
  while (top.depth !== 1 && guard++ < 100) {
    const parent = top.requester_session_key
      ? parentByChildSession.get(top.requester_session_key)
      : undefined;
    if (!parent) break;
    top = parent;
  }

  // 收集该调用 session 下所有 depth-1 根任务
  const callingSid = top.requester_session_key ?? top.child_session_key;
  const roots = pool.filter(
    r => r.depth === 1 && r.requester_session_key === callingSid,
  );
  if (roots.length === 0) return [];

  // 对每个根任务收集其整棵子树（根 + 后代），按根在前、后代入内的 BFS 顺序汇总
  const seen = new Set<string>();
  const result: SubagentRun[] = [];
  const appendTree = (root: SubagentRun): void => {
    if (seen.has(root.run_id)) return;
    seen.add(root.run_id);
    result.push(root);
    const queue: string[] = [];
    if (root.child_session_key) queue.push(root.child_session_key);
    while (queue.length > 0) {
      const key = queue.shift();
      if (!key) continue;
      const children = byRequester.get(key);
      if (!children) continue;
      for (const child of children) {
        if (seen.has(child.run_id)) continue;
        seen.add(child.run_id);
        result.push(child);
        if (child.child_session_key) queue.push(child.child_session_key);
      }
    }
  };
  for (const r of roots) appendTree(r);
  return result;
});

/* ------------------------------------------------------------------ */
/* 公共 API                                                             */
/* ------------------------------------------------------------------ */

export function useSubagentTasks() {
  const { t } = useI18n();

  /** 运行记录的状态徽章样式（按 ExecutionStatus / RunOutcomeStatus 上色） */
  function badgeClass(run: SubagentRun): string {
    const exec = run?.execution?.status;
    if (exec === 'RUNNING') return 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300';
    if (exec === 'INTERRUPTED') return 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300';
    const outcome = run?.execution?.outcome?.status;
    if (outcome === 'OK') return 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300';
    if (outcome === 'ERROR') return 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300';
    if (outcome === 'TIMEOUT' || outcome === 'KILLED')
      return 'bg-orange-100 text-orange-700 dark:bg-orange-900/40 dark:text-orange-300';
    return 'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-300';
  }

  /** 状态文案（优先运行态，其次配送态，最后结果态） */
  function statusLabel(run: SubagentRun): string {
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
  }

  /** 角色标签：root / 直属子任务 等 */
  function roleLabel(run: SubagentRun): string {
    const depth = run?.depth ?? 0;
    if (depth <= 0) return t('sidebar.roleRoot');
    return `${t('sidebar.roleChild')}#${depth}`;
  }

  /** 运行条目主标题：优先 label/task_name，其次 task 文本 */
  function runLabel(run: SubagentRun): string {
    return run?.label || run?.task_name || run?.task || run?.run_id || '-';
  }

  /** 调用方会话：展示发起该子任务的父 session_id（requester_session_key） */
  function parentSessionLabel(run: SubagentRun): string {
    return run?.requester_session_key || '-';
  }

  /** 上次更新时间文案（秒级） */
  const lastUpdatedText = computed(() => {
    if (!lastTasksFetchedAt.value) return '';
    const sec = Math.max(0, Math.floor((Date.now() - lastTasksFetchedAt.value) / 1000));
    return t('sidebar.tasksAgoSeconds', { sec });
  });

  /**
   * 初始化：订阅 WS（单例）+ 拉取当前会话任务。
   * 每个消费组件在 onMounted 调用即可（幂等）。传入当前 sid 用于首次加载。
   */
  function initTasks(sid?: string): void {
    setupSubagentWs();
    // 拉取「仍存在会话」集合，为 orphaned run 过滤做好准备（幂等）
    void loadSubagentValidSessions();
    const target = resolveSid(sid);
    if (target && lastLoadedSessionId !== target) {
      lastLoadedSessionId = target;
      void loadTaskRuns(target);
    } else if (target && taskRuns.value.length === 0) {
      void loadTaskRuns(target);
    } else if (!target) {
      // 无 sid（根路径）：清空列表
      taskRuns.value = [];
    }
  }

  /** 手动触发一次加载（如切会话后由父组件调用）。 */
  function refresh(sid?: string): void {
    const target = resolveSid(sid);
    if (target) {
      lastLoadedSessionId = target;
      void loadTaskRuns(target);
    }
  }

  /**
   * 仅刷新当前聚焦 task box（focused run 子树）的图数据。
   *
   * 1) 若当前有聚焦 run：拉取该 run + 其整棵子树（GET /subagents/runs?run_id=…，
   *    由后端返回 root + descendants），随后将这批记录回写 Dexie 缓存并重建列表，
   *    focusedSubtreeRuns 会自动重算，从而只重绘当前聚焦子树，而非整个会话。
   * 2) 若未聚焦（图展示整树）：回退为按当前会话做一次全量刷新。
   */
  async function refreshFocusedSubtree(): Promise<void> {
    const rootId = focusedRunId.value;
    // 未聚焦时回退会话级全量刷新
    if (!rootId) {
      refresh(resolveSid());
      return;
    }
    taskLoading.value = true;
    try {
      const runs = await fetchSubagentRunSubtree(rootId);
      if (!runs.length) return;
      // 将子树记录写回 Dexie（后续 WS / 其它会话视图也能拿到最新状态）
      await cacheSubagentRuns(runs.map(toCachedSubagentRun));
      // 重建列表（读全量缓存 → 同步 allTaskRuns，由 focusedSubtreeRuns 派生重算）
      await refreshFromCache();
      lastTasksFetchedAt.value = Date.now();
    } catch (e) {
      console.error('[useSubagentTasks] 刷新聚焦 task box 子树失败：', e);
    } finally {
      taskLoading.value = false;
    }
  }

  /**
   * 明确标记「后台任务」是否处于展示态，供 ready 事件只在实际查看时拉取全量。
   * 侧边栏切到 tasks / 任务视图展示时应置 true；切回「会话」置 false。
   */
  function setTasksTabActive(active: boolean): void {
    tasksTabActive.value = active;
  }

  /* ------------------------------------------------------------------ */
  /* 后台任务 tab 多选 / 全选 / 批量删除                                  */
  /* ------------------------------------------------------------------ */

  /** 当前可选（第一层直属任务）run_id 列表。 */
  function selectableRunIds(): string[] {
    return rootTaskRuns.value.map(r => r.run_id).filter(Boolean);
  }

  /** 是否已全选（非空且全部选中）。 */
  const allSelected = computed(() => {
    const ids = selectableRunIds();
    return ids.length > 0 && ids.every(id => selectedRunIds.value.has(id));
  });

  /** 是否处于半选态（有选但未全选）。 */
  const someSelected = computed(() => {
    const ids = selectableRunIds();
    return ids.some(id => selectedRunIds.value.has(id)) && !allSelected.value;
  });

  /** 切换单个任务选中态。 */
  function toggleTaskSelection(runId: string): void {
    selectedRunIds.value = new Set(selectedRunIds.value);
    if (selectedRunIds.value.has(runId)) selectedRunIds.value.delete(runId);
    else selectedRunIds.value.add(runId);
  }

  /** 全选 / 取消全选第一层直属任务。 */
  function toggleSelectAllTasks(): void {
    const ids = selectableRunIds();
    if (allSelected.value) selectedRunIds.value = new Set();
    else selectedRunIds.value = new Set(ids);
  }

  /** 清空选中（删除完成后调用）。 */
  function clearTaskSelection(): void {
    selectedRunIds.value = new Set();
  }

  /** 收集指定根 run 及其所有后代 run_id（父子：父 child_session_key === 子 requester_session_key）。 */
  function collectSubtreeRunIds(rootId: string, pool: SubagentRun[]): string[] {
    const root = pool.find(r => r.run_id === rootId);
    if (!root) return [rootId];
    const out: string[] = [root.run_id];
    const byRequester = new Map<string, SubagentRun[]>();
    for (const run of pool) {
      const key = run.requester_session_key;
      if (!key) continue;
      const list = byRequester.get(key);
      if (list) list.push(run);
      else byRequester.set(key, [run]);
    }
    const queue: string[] = [];
    if (root.child_session_key) queue.push(root.child_session_key);
    while (queue.length > 0) {
      const key = queue.shift();
      if (!key) continue;
      const children = byRequester.get(key);
      if (!children) continue;
      for (const child of children) {
        out.push(child.run_id);
        if (child.child_session_key) queue.push(child.child_session_key);
      }
    }
    return out;
  }

  /**
   * 删除一个根任务及其整棵子树（前后端彻底清空）。
   *
   * 1) 调后端 DELETE 端点，清空内存注册表 + SQLite + 附件目录；
   * 2) 从 store 的 taskRuns / allTaskRuns 中移除根+所有后代；
   * 3) 从 Dexie 缓存 bulkDelete，保证前后端一致清空。
   *
   * @param runId 要删除的根 run_id。
   */
  async function deleteSubagentSubtree(runId: string): Promise<void> {
    if (deletingRunIds.value.has(runId)) return;
    deletingRunIds.value = new Set(deletingRunIds.value).add(runId);
    try {
      // 预先基于当前 store 数据计算要移除的整棵子树 id
      const pool = allTaskRuns.value;
      const targetIds = collectSubtreeRunIds(runId, pool);
      // 1) 后端删除
      await deleteSubagentRunSubtree(runId);
      // 2) store 内移除
      const removed = new Set(targetIds);
      taskRuns.value = taskRuns.value.filter(r => !removed.has(r.run_id));
      allTaskRuns.value = allTaskRuns.value.filter(r => !removed.has(r.run_id));
      // 3) 清 Dexie
      try {
        await deleteCachedSubagentRuns(targetIds);
      } catch (e) {
        console.warn('[useSubagentTasks] 清除本地子任务缓存失败：', e);
      }
      // 若聚焦/展开/选中的节点被删除，则一并清理相关状态
      if (focusedRunId.value && removed.has(focusedRunId.value)) focusedRunId.value = undefined;
      if (expandedRunId.value && removed.has(expandedRunId.value)) {
        expandedRunId.value = undefined;
        selectedRunId.value = undefined;
      }
      selectedRunIds.value = new Set(
        [...selectedRunIds.value].filter(id => !removed.has(id)),
      );
    } catch (e) {
      console.error('[useSubagentTasks] 删除子 Agent 子树失败：', e);
      throw e;
    } finally {
      deletingRunIds.value = new Set(deletingRunIds.value);
      deletingRunIds.value.delete(runId);
    }
  }

  /** 批量删除当前已选中的第一层任务（各自删除其根子树）。 */
  async function deleteSelectedTasks(): Promise<number> {
    const ids = [...selectedRunIds.value];
    let removed = 0;
    for (const id of ids) {
      try {
        await deleteSubagentSubtree(id);
        removed += 1;
      } catch {
        // 单个失败不中断其余任务删除
      }
    }
    clearTaskSelection();
    return removed;
  }

  return {
    // 响应式状态
    taskRuns,
    allTaskRuns,
    rootTaskRuns,
    groupedRootTaskRuns,
    focusedSubtreeRuns,
    taskLoading,
    lastTasksFetchedAt,
    subagentWsReady,
    runningTaskCount,
    allRunningTaskCount,
    lastUpdatedText,
    // 任务视图展开/流程图状态（跨 chat↔tasks 切换保活）
    expandedRunId,
    selectedRunId,
    focusedRunId,
    toggleExpandRun,
    focusRun,
    resetFlowState,
    // 行为方法
    isRunning,
    badgeClass,
    statusLabel,
    roleLabel,
    runLabel,
    parentSessionLabel,
    initTasks,
    refresh,
    refreshFocusedSubtree,
    setTasksTabActive,
    // 后台任务 tab 多选/全选/批量删除
    selectedRunIds,
    deletingRunIds,
    allSelected,
    someSelected,
    toggleTaskSelection,
    toggleSelectAllTasks,
    clearTaskSelection,
    deleteSubagentSubtree,
    deleteSelectedTasks,
    // 底层复用（供 SubagentTasksView 等做内部处理）
    loadTaskRuns,
    refreshFromCache,
    toSubagentRun,
    // 会话键归一化 + 有效会话集合（供「返回会话」按钮校验 + orphaned run 过滤复用）
    normalizeSessionKey,
    loadSubagentValidSessions,
    validSessionIds: subagentValidSessionIds
  };
}
