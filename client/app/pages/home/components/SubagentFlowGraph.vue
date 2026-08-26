<template>
  <div class="flex flex-col h-full w-full bg-[#f8f9fa] dark:bg-[#131619]">
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
import { Graph, NodeEvent } from '@antv/g6';
import type { GraphData, IElementEvent } from '@antv/g6';
import { on, off } from '@/composables/mitt';
import { fetchSubagentRuns, type SubagentRun } from '@/composables/bridge';
import { useSubagentWs } from '@/composables/ws';

const { t } = useI18n();

/** 当前会话 id（由父组件 v-model:current-session-id 双向同步） */
const currentSessionId = defineModel<string | undefined>('currentSessionId');

/** 选中的 run id（由父组件 :selected-run-id 传入，用于高亮 + 作为根展示其全部后代） */
const selectedRunId = defineModel<string | undefined>('selectedRunId');

/** 选中的完整 run 对象（由父组件 v-model:selected-run 传入，供下方详情面板展示） */
const selectedRun = defineModel<SubagentRun | undefined>('selectedRun');

/** 供下方详情面板使用的导航提示（点击节点时由本组件外抛） */
const emit = defineEmits<{
  (e: 'node-select', run: SubagentRun | undefined): void;
}>();

const containerRef = ref<HTMLDivElement | null>(null);
const graphRef = shallowRef<Graph | null>(null);
const resizeObserverRef = shallowRef<ResizeObserver | null>(null);
const loading = ref(false);
const empty = ref(false);
const error = ref(false);

/** 内部权威数据源：run_id → SubagentRun（由 fetch + WS 增量累积） */
const runStore = new Map<string, SubagentRun>();

/**
 * 请求序号：用于丢弃「迟到的旧响应」。
 * loadFlow 会被 onMounted / watch(currentSessionId) / ws:subagents:ready /
 * watch(colorMode) 等多个路径触发，快速切换会话/tab 时会并发多个 async 拉取；
 * 若无防护，旧会话的响应（可能为空）会晚到并覆盖新会话正在展示的运行树，
 * 导致间歇性显示「暂无子 Agent 运行」。每次 loadFlow 递增该序号，仅接受最新一次。
 */
let loadFlowSeq = 0;

/**
 * 可选：外部指定要展示的运行集（如侧边栏点击聚焦某任务后，仅展示该任务的子任务子树）。
 * 当该 prop 有值时（且非空），图谱只渲染这里给定的 run，忽略内部 runStore 的整棵会话树；
 * 无值时回退为渲染 runStore 的完整运行树（未聚焦时的「总树状图」行为）。
 */
const displayRuns = defineModel<SubagentRun[] | undefined>('displayRuns');

const colorMode = useColorMode();

/** 当前是否为深色主题 */
const isDark = () => colorMode.value === 'dark';

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
 * 将运行记录列表映射为 G6 图数据（树形）。
 * 父子关联（SubagentRun 无 parent_run_id）：父 run 的 child_session_key = K，
 * 则所有 requester_session_key === K 的 run 都是其直接子任务。由此逐层派生边。
 * @param runs 运行记录列表
 * 计算单个 run 的节点基础样式（按运行状态着色）。
 * 选中高亮不在此处理，改由 G6 的 selected state 就地应用（见 ensureGraph 的 node.state 配置），
 * 从而避免点击节点时重建整图 / 重跑布局。
 * @param run 运行记录
 */
const nodeStyle = (run: SubagentRun) => {
  return {
    fill: isDark() ? '#1a1d21' : '#ffffff',
    stroke: statusColor(run?.execution?.status ?? ''),
    lineWidth: 2,
    size: 32
  };
};

/**
 * 将运行集映射为 G6 GraphData。
 * @param runs 待展示的运行集
 */
const mapToGraphData = (runs: SubagentRun[]): GraphData => {
  const nodes: GraphData['nodes'] = [];
  const edges: GraphData['edges'] = [];
  const seen = new Set<string>();
  // child_session_key → run_id：用于把子任务的 requester_session_key 反查到父 run
  const childSessionKeyToRunId = new Map<string, string>();

  for (const run of runs) {
    const id = run.run_id;
    if (!id || seen.has(id)) continue;
    seen.add(id);
    if (run.child_session_key) childSessionKeyToRunId.set(run.child_session_key, id);

    const status = statusKey(run);
    nodes.push({
      id,
      label: nodeLabel(run),
      data: {
        status,
        depth: run.depth ?? 0,
        role: run.role ?? null
      },
      style: nodeStyle(run)
    });
  }

  // 边：父 run → 子 run。子 run 的 requester_session_key 对应父 run 的 child_session_key，
  // 通过反查表找到父 run_id；找不到（requester 是会话根、不在节点集内）则跳过，避免 G6 "Node not found"。
  for (const run of runs) {
    const id = run.run_id;
    if (!id || !seen.has(id)) continue;
    const parentId = childSessionKeyToRunId.get(run.requester_session_key);
    if (!parentId || parentId === id) continue;
    edges.push({
      id: `${parentId}->${id}`,
      source: parentId,
      target: id,
      style: {
        stroke: isDark() ? '#4b5563' : '#9ca3af',
        lineWidth: 1.5,
        endArrow: true
      }
    });
  }

  return { nodes, edges };
};

/**
 * 从展示数据源派生 GraphData。
 * - 当外部通过 displayRuns 指定了运行集（侧边栏聚焦某任务）时：此时以该任务的子树为「权威范围」，
 *   图谱只渲染这批 run，避免把同会话的其它无关兄弟任务也画进来。
 * - 未指定 displayRuns 时：渲染 runStore 的完整运行树（默认「总树状图」）。
 * 高亮（selected state）由 ensureGraph / applyHighlight 就地管理，不参与数据派生。
 */
const deriveDisplayData = (): GraphData => {
  const sourceRuns = displayRuns.value?.length
    ? displayRuns.value
    : Array.from(runStore.values());
  return mapToGraphData(sourceRuns);
};

/**
 * 构建 G6 图配置并渲染（仅在尚无实例或图结构发生实质性变化时调用）。
 * 结构变化（首次加载 / WS 引起新增节点）会重建数据并重新执行布局；
 * 单纯的选中高亮切换应调用 applyHighlight()，避免整图重建与重排。
 */
const ensureGraph = async (data: GraphData) => {
  if (!containerRef.value) return;

  // 已有实例：节点/边集合整体变化（首次加载之外的全部场景：task box 切换 / session 切换 / WS 增量）。
  // 不能在已有实例上 setData 引入全新节点后再重跑 d3-force layout()——
  // G6 v5 该路径下 before-layout 触发但 after-layout 永不触发，力导向模拟永不收敛，
  // 全新节点坐标全程停留在 [0,0]，互相堆叠（已端到端复现确认）。
  // 因此销毁实例并按新的权威数据重建，确保 d3-force 依据新拓扑完整初始化每个节点位置。
  if (graphRef.value) {
    destroyGraph();
  }

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
      },
      state: {
        selected: {
          fill: dark ? '#1e3a5f' : '#dbeafe',
          stroke: dark ? '#60a5fa' : '#2563eb',
          lineWidth: 4,
          size: 40
        }
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

  // 点击节点：选中并联动下方详情面板
  // G6 v5 的 node:click 事件中 evt.target 已由 forceCanvas 事件转发层的
  // eventTargetOf() 解析为节点元素本身（其 id 即 run_id），无论点中节点主体
  // 还是其 label 子形状都一样。因此用 targetType 判定命中节点后取 evt.target.id 即可。
  graph.on(NodeEvent.CLICK, (evt: IElementEvent) => {
    // G6 的 node:click 会解析 evt.target 为节点元素（id 即 run_id），点击 label 子形状时同样命中节点本体
    const id = evt.targetType === 'node' ? evt.target.id : undefined;
    if (!id) return;
    // 优先从实际渲染数据源解析 run：焦点子树（displayRuns）可能是跨会话的，
    // 其节点 id 未必存在于仅含当前会话 descendants 的 runStore 中；故二者都查。
    const run = displayRuns.value?.length
      ? displayRuns.value.find((r) => r.run_id === id)
      : runStore.get(id);
    if (run) {
      selectedRunId.value = id;
      selectedRun.value = run;
      emit('node-select', run);
    }
  });

  graphRef.value = graph;
  await graph.render();
  // 容器尺寸变化时同步 G6 canvas 宽高（保持 100% 跟随父容器）。
  // G6 v5 无参 resize() 并非稳定支持，需显式传入容器像素尺寸，canvas 才会真正跟随缩放。
  resizeObserverRef.value?.disconnect();
  resizeObserverRef.value = new ResizeObserver(() => {
    if (graphRef.value && containerRef.value) {
      try {
        const { clientWidth, clientHeight } = containerRef.value;
        graphRef.value.resize(clientWidth || 1, clientHeight || 1);
      } catch {
        /* canvas 尚未就绪，忽略 */
      }
    }
  });
  if (containerRef.value) resizeObserverRef.value.observe(containerRef.value);
  // 首次渲染后重放既有选中（若存在），使高亮即时可见
  if (selectedRunId.value) {
    try {
      await graph.setElementState(selectedRunId.value, ['selected']);
    } catch {
      /* 选中节点不在新数据内，忽略 */
    }
  }
};

/** 销毁当前图实例（用于更换会话/清空数据时释放） */
const destroyGraph = () => {
  resizeObserverRef.value?.disconnect();
  resizeObserverRef.value = null;
  graphRef.value?.destroy();
  graphRef.value = null;
};

/**
 * 选中高亮切换：就地应用 G6 的 selected state，不销毁实例、不重跑布局。
 * 依赖 ensureGraph 中配置的 node.state.selected 样式，点击节点后仅改变该节点状态即可即时高亮。
 * @param prev 上一个选中节点 id（撤销高亮），可为空
 * @param next 新选中的节点 id（点亮），可为空
 */
const applyHighlight = async (prev?: string, next?: string) => {
  const graph = graphRef.value;
  if (!graph) return;
  if (prev && prev !== next) {
    try {
      await graph.setElementState(prev, []);
    } catch {
      /* 节点可能已不在当前展示范围，忽略 */
    }
  }
  if (next) {
    try {
      await graph.setElementState(next, ['selected']);
    } catch {
      /* 节点可能尚未渲染，忽略（后续 render 时会据 selectedRunId 补齐） */
    }
  }
};

/** 拉取并渲染子 Agent 运行树（写入 runStore 后按选中状态派生展示） */
const loadFlow = async () => {
  const sid = currentSessionId.value;
  if (!sid) return;
  // 本次请求的序号：任何旧请求在 await 返回后若发现新一轮 loadFlow 已开始，
  // 或当前会话已切换，则直接丢弃其结果，防止旧数据覆盖新会话（间歇性空态根因）。
  const seq = ++loadFlowSeq;
  loading.value = true;
  error.value = false;
  empty.value = false;
  try {
    const runs = await fetchSubagentRuns(sid, 'descendants');
    // 竞态守卫：已有更新的 loadFlow 发起，或会话已变，丢弃本次结果
    if (seq !== loadFlowSeq || sid !== currentSessionId.value) return;
    runStore.clear();
    for (const run of runs) {
      if (run?.run_id) runStore.set(run.run_id, run);
    }
    if (runStore.size === 0) {
      empty.value = true;
      destroyGraph();
      return;
    }
    ensureGraph(deriveDisplayData());
  } catch (e) {
    // 竞态守卫同样适用于错误分支：被更新的请求取代时，不污染当前状态
    if (seq !== loadFlowSeq || sid !== currentSessionId.value) return;
    console.error('[SubagentFlowGraph] 拉取子 Agent 运行树失败：', e);
    error.value = true;
    destroyGraph();
  } finally {
    if (seq === loadFlowSeq) loading.value = false;
  }
};

/** 增量更新：合并新事件到 runStore 并按选中状态重渲染 */
const applyIncremental = (run: SubagentRun) => {
  if (!run?.run_id) return;
  if (empty.value) empty.value = false;
  if (error.value) error.value = false;

  // 写入权威数据源
  runStore.set(run.run_id, run);

  // 从 runStore 派生展示数据（始终渲染完整运行树，仅高亮当前选中）
  ensureGraph(deriveDisplayData());
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
    selectedRun.value = undefined;
    loadFlow();
  }
);

/**
 * 外部展示范围变化（侧边栏聚焦切换：displayRuns 在全树/某任务子树间切换）。
 * 节点/边的集合与结构随之变化，需重建数据并重新布局；高亮由 applyHighlight 就地更新。
 */
watch(
  () => displayRuns.value,
  () => {
    if (!empty.value && !error.value && graphRef.value) {
      ensureGraph(deriveDisplayData());
    }
  },
  { deep: true }
);

/** 选中 run 变化时只就地更新高亮（selected state），不重建整图、不重排布局 */
watch(
  () => selectedRunId.value,
  (next, prev) => {
    if (!empty.value && !error.value && graphRef.value) {
      applyHighlight(prev, next);
    }
  }
);

onMounted(() => {
  setupSubagentWs();
  loadFlow();
});

onBeforeUnmount(() => {
  teardownSubagentSubscribe();
  destroyGraph();
});
</script>
