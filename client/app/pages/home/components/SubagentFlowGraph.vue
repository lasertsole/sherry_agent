<template>
  <div class="flex flex-col h-full w-full bg-[#f8f9fa] dark:bg-[#131619]">
    <!-- 面板头部：标题 + 折叠/关闭 -->
    <div
      class="shrink-0 h-14 flex items-center gap-2 px-4 border-b border-solid border-gray-light dark:border-gray-dark bg-white dark:bg-[#1a1d21]">
      <i class="pi pi-sitemap text-theme-main" />
      <span class="text-base font-semibold text-gray-900 dark:text-gray-100">
        {{ t('flow.title') }}
      </span>
      <div class="ml-auto flex items-center gap-1">
        <Button
          :icon="collapsed ? 'pi pi-angle-double-left' : 'pi pi-angle-double-right'"
          :title="collapsed ? t('flow.expand') : t('flow.collapse')"
          :aria-label="collapsed ? t('flow.expand') : t('flow.collapse')"
          variant="text"
          rounded
          @click="toggleCollapsed" />
        <Button
          icon="pi pi-times"
          :title="t('flow.close')"
          :aria-label="t('flow.close')"
          variant="text"
          rounded
          @click="closePanel" />
      </div>
    </div>

    <!-- 图谱渲染区 -->
    <div class="relative flex-1 w-full h-full overflow-hidden">
      <div ref="containerRef" class="w-full h-full" />

      <!-- 加载中 -->
      <div
        v-if="loading"
        class="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-white/60 dark:bg-[#131619]/60">
        <i class="pi pi-spin pi-spinner text-3xl text-gray-400 dark:text-gray-500" />
        <span class="text-sm text-gray-500 dark:text-gray-400">{{ t('flow.loading') }}</span>
      </div>

      <!-- 空态 -->
      <div
        v-else-if="empty"
        class="absolute inset-0 flex flex-col items-center justify-center gap-4">
        <i class="pi pi-sitemap text-6xl text-gray-300 dark:text-gray-600" />
        <div class="text-base text-gray-500 dark:text-gray-400">
          {{ t('flow.empty') }}
        </div>
      </div>

      <!-- 错误态 -->
      <div
        v-else-if="error"
        class="absolute inset-0 flex flex-col items-center justify-center gap-4">
        <i class="pi pi-exclamation-triangle text-5xl text-red-400 dark:text-red-500" />
        <div class="text-base text-gray-500 dark:text-gray-400">
          {{ t('flow.error') }}
        </div>
      </div>
    </div>
  </div>
</template>

<script lang="ts" setup>
import { ref, shallowRef, watch, onMounted, onBeforeUnmount } from 'vue';
import { useI18n } from 'vue-i18n';
import { Graph } from '@antv/g6';
import type { GraphData } from '@antv/g6';
import { on, off } from '@/composables/mitt';
import { fetchSubagentRuns, type SubagentRun } from '@/composables/bridge';
import { useSubagentWs } from '@/composables/ws';

const { t } = useI18n();

/** 是否折叠（由父组件 v-model:collapsed 控制） */
const collapsed = defineModel<boolean>('collapsed', { default: false });

/** 当前会话 id（由父组件 v-model:current-session-id 双向同步） */
const currentSessionId = defineModel<string | undefined>('currentSessionId');

/** 关闭面板（由父组件 v-model:visible 控制） */
const visible = defineModel<boolean>('visible', { default: true });

/** 选中的 run id（由父组件 :selected-run-id 传入，用于高亮 + 作为根展示其全部后代） */
const selectedRunId = defineModel<string | undefined>('selectedRunId');

const containerRef = ref<HTMLDivElement | null>(null);
const graphRef = shallowRef<Graph | null>(null);
const loading = ref(false);
const empty = ref(false);
const error = ref(false);

/** 内部权威数据源：run_id → SubagentRun（由 fetch + WS 增量累积） */
const runStore = new Map<string, SubagentRun>();

const colorMode = useColorMode();

/** 当前是否为深色主题 */
const isDark = () => colorMode.value === 'dark';

/** 折叠/展开面板 */
const toggleCollapsed = () => {
  collapsed.value = !collapsed.value;
};

/** 关闭面板 */
const closePanel = () => {
  visible.value = false;
};

/** 运行状态 → 节点颜色（浅色主题） */
const statusColorLight = (status: string): string => {
  switch (status) {
    case 'RUNNING':
    case 'INTERRUPTED':
      return '#3b82f6';
    case 'OK':
      return '#10b981';
    case 'ERROR':
    case 'TIMEOUT':
    case 'KILLED':
      return '#ef4444';
    default:
      return '#64748b';
  }
};

/** 运行状态 → 节点颜色（深色主题） */
const statusColorDark = (status: string): string => {
  switch (status) {
    case 'RUNNING':
    case 'INTERRUPTED':
      return '#60a5fa';
    case 'OK':
      return '#34d399';
    case 'ERROR':
    case 'TIMEOUT':
    case 'KILLED':
      return '#f87171';
    default:
      return '#94a3b8';
  }
};

/** 运行状态 → 节点颜色（按主题） */
const statusColor = (status: string): string => {
  return isDark() ? statusColorDark(status) : statusColorLight(status);
};

/** 运行状态 → 状态文案 key */
const statusKey = (run: SubagentRun): string => {
  const exec = run?.execution?.status;
  if (exec === 'RUNNING' || exec === 'INTERRUPTED') return 'running';
  const outcome = run?.execution?.outcome?.status;
  if (outcome === 'OK') return 'completed';
  if (outcome === 'ERROR' || outcome === 'TIMEOUT' || outcome === 'KILLED') return 'failed';
  return 'unknown';
};

/** 节点主标题：优先 label/task_name，其次 task 文本 */
const nodeLabel = (run: SubagentRun): string => {
  return run?.label || run?.task_name || run?.task || run?.run_id || '-';
};

/**
 * 将运行记录列表映射为 G6 图数据（树形：requester → child）。
 * @param runs 运行记录列表
 * @param highlightId 需要高亮的节点 id（选中 run），无则不高亮
 */
const mapToGraphData = (runs: SubagentRun[], highlightId?: string): GraphData => {
  const nodes: GraphData['nodes'] = [];
  const edges: GraphData['edges'] = [];
  const seen = new Set<string>();

  for (const run of runs) {
    const id = run.run_id;
    if (!id || seen.has(id)) continue;
    seen.add(id);

    const status = statusKey(run);
    const isHighlight = highlightId !== undefined && id === highlightId;
    nodes.push({
      id,
      label: nodeLabel(run),
      data: {
        status,
        depth: run.depth ?? 0,
        role: run.role ?? null
      },
      style: {
        fill: isHighlight
          ? (isDark() ? '#1e3a5f' : '#dbeafe')
          : (isDark() ? '#1a1d21' : '#ffffff'),
        stroke: isHighlight
          ? (isDark() ? '#60a5fa' : '#2563eb')
          : statusColor(run?.execution?.status ?? ''),
        lineWidth: isHighlight ? 4 : 2,
        size: isHighlight ? 40 : 32
      }
    });

    // 边：requester_session_key → child_session_key
    // requester 是会话 sid 时表示该 run 是树的根（来源不在 nodes 集合内，无父节点），不建边，
    // 否则 G6 会因找不到 source 节点而抛 "Node not found" 异常导致整个图加载失败
    const source = run.requester_session_key;
    if (source && source !== id && seen.has(source)) {
      edges.push({
        id: `${source}->${id}`,
        source,
        target: id,
        style: {
          stroke: isDark() ? '#4b5563' : '#9ca3af',
          lineWidth: 1.5,
          endArrow: true
        }
      });
    }
  }

  return { nodes, edges };
};

/**
 * 从 runStore 中计算以 selectedId 为根的完整后代子树（含 selectedId 自身）。
 * 祖先节点被排除；边方向保持 requester → child 不变。
 */
const computeSubtree = (selectedId: string): SubagentRun[] => {
  const result: SubagentRun[] = [];
  const visited = new Set<string>();
  const queue: string[] = [selectedId];

  while (queue.length > 0) {
    const current = queue.shift()!;
    if (visited.has(current)) continue;
    visited.add(current);
    const run = runStore.get(current);
    if (!run) continue;
    result.push(run);
    // 收集以 current 为 requester 的所有后代
    for (const [childId, childRun] of runStore) {
      if (childRun.requester_session_key === current && !visited.has(childId)) {
        queue.push(childId);
      }
    }
  }

  return result;
};

/** 根据当前选中状态从 runStore 派生展示数据 */
const deriveDisplayData = (): GraphData => {
  const selected = selectedRunId.value;
  if (selected && runStore.has(selected)) {
    return mapToGraphData(computeSubtree(selected), selected);
  }
  return mapToGraphData(Array.from(runStore.values()));
};

/** 构建 G6 图配置并渲染 */
const renderGraph = (data: GraphData) => {
  if (!containerRef.value) return;

  graphRef.value?.destroy();
  graphRef.value = null;

  const dark = isDark();
  const background = dark ? '#131619' : '#f8f9fa';
  const labelFill = dark ? '#e5e7eb' : '#1f2937';

  const graph = new Graph({
    container: containerRef.value,
    data,
    background,
    cursor: 'grab',
    zoom: 0.8,
    autoFit: 'view',
    node: {
      style: {
        labelText: (datum: { label?: string }) => datum.label ?? '',
        labelFill,
        labelFontSize: 11,
        labelPlacement: 'bottom',
        labelOffsetY: 6
      }
    },
    edge: {
      style: {
        stroke: dark ? '#4b5563' : '#9ca3af',
        lineWidth: 1.5,
        endArrow: true
      }
    },
    behaviors: ['drag-canvas', 'zoom-canvas', 'drag-element'],
    layout: {
      type: 'd3-force',
      collide: {
        radius: 50
      },
      link: {
        distance: 120
      },
      manyBody: {
        strength: -200
      }
    }
  });

  graphRef.value = graph;
  graph.render();
};

/** 拉取并渲染子 Agent 运行树（写入 runStore 后按选中状态派生展示） */
const loadFlow = async () => {
  const sid = currentSessionId.value;
  if (!sid) return;
  loading.value = true;
  error.value = false;
  empty.value = false;
  try {
    const runs = await fetchSubagentRuns(sid, 'descendants');
    runStore.clear();
    for (const run of runs) {
      if (run?.run_id) runStore.set(run.run_id, run);
    }
    if (runStore.size === 0) {
      empty.value = true;
      graphRef.value?.destroy();
      graphRef.value = null;
      return;
    }
    renderGraph(deriveDisplayData());
  } catch (e) {
    console.error('[SubagentFlowGraph] 拉取子 Agent 运行树失败：', e);
    error.value = true;
    graphRef.value?.destroy();
    graphRef.value = null;
  } finally {
    loading.value = false;
  }
};

/** 增量更新：合并新事件到 runStore 并按选中状态重渲染 */
const applyIncremental = (run: SubagentRun) => {
  if (!run?.run_id) return;
  if (empty.value) empty.value = false;
  if (error.value) error.value = false;

  // 写入权威数据源
  runStore.set(run.run_id, run);

  // 从 runStore 派生展示数据（选中态下自动过滤到选中 run 的后代子树）
  renderGraph(deriveDisplayData());
};

/** 建立 /subagents/ws 订阅（复用模块级单例，不新建连接） */
const setupSubagentWs = () => {
  useSubagentWs({
    onReconnect: () => {
      // 重连成功后服务端会补发 ready，届时再拉一次全量补齐
    }
  });

  on('ws:subagent_spawned', (payload: unknown) => {
    applyIncremental(payload as SubagentRun);
  });

  on('ws:subagent_ended', (payload: unknown) => {
    applyIncremental(payload as SubagentRun);
  });

  on('ws:subagents:ready', () => {
    loadFlow();
  });
};

/** 移除 mitt 订阅（WS 连接为模块级单例，交给后续组件复用，不在此关闭） */
const teardownSubagentSubscribe = () => {
  off('ws:subagent_spawned');
  off('ws:subagent_ended');
  off('ws:subagents:ready');
};

/** 主题切换时重建图谱以应用新配色 */
watch(
  () => colorMode.value,
  () => {
    if (!empty.value && !error.value && graphRef.value) {
      loadFlow();
    }
  }
);

/** 会话切换时清空选中并重新拉取该会话的运行树 */
watch(
  () => currentSessionId.value,
  () => {
    selectedRunId.value = undefined;
    if (visible.value) loadFlow();
  }
);

/** 选中 run 变化时按新选中根重渲染（null → 全量树） */
watch(
  () => selectedRunId.value,
  () => {
    if (visible.value && !empty.value && !error.value && runStore.size > 0) {
      renderGraph(deriveDisplayData());
    }
  }
);

onMounted(() => {
  setupSubagentWs();
  loadFlow();
});

onBeforeUnmount(() => {
  teardownSubagentSubscribe();
  graphRef.value?.destroy();
  graphRef.value = null;
});
</script>
