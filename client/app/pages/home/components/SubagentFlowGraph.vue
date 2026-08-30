<template>
  <div class="flex flex-col h-full w-full bg-[#f8f9fa] dark:bg-[#131619]">
    <!-- Graph rendering area -->
    <div class="relative flex-1 w-full h-full overflow-hidden">
      <div
        ref="containerRef"
        class="w-full h-full" />

      <!-- Loading -->
      <div
        v-if="loading"
        class="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-white/60 dark:bg-[#131619]/60">
        <i class="pi pi-spin pi-spinner text-3xl text-gray-400 dark:text-gray-500" />
        <span class="text-sm text-gray-500 dark:text-gray-400">{{ t('flow.loading') }}</span>
      </div>

      <!-- Empty state -->
      <div
        v-else-if="empty"
        class="absolute inset-0 flex flex-col items-center justify-center gap-4">
        <i class="pi pi-sitemap text-6xl text-gray-300 dark:text-gray-600" />
        <div class="text-base text-gray-500 dark:text-gray-400">
          {{ t('flow.empty') }}
        </div>
      </div>

      <!-- Error state -->
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
import type { GraphData, IElementEvent, NodeData } from '@antv/g6';
import { on, off } from '@/composables/mitt';
import { fetchSubagentRuns, type SubagentRun } from '@/composables/bridge';
import { useSubagentWs } from '@/composables/ws';

  const { t } = useI18n();

/** Current session id (two-way synced from the parent via v-model:current-session-id) */
const currentSessionId = defineModel<string | undefined>('currentSessionId');

/** Selected run id (passed in from the parent via :selected-run-id, used for highlighting + as the root for showing all of its descendants) */
const selectedRunId = defineModel<string | undefined>('selectedRunId');

/** The complete selected run object (passed in from the parent via v-model:selected-run, displayed in the detail panel below) */
const selectedRun = defineModel<SubagentRun | undefined>('selectedRun');

/** Navigation hint for the detail panel below (emitted outward by this component when a node is clicked) */
const emit = defineEmits<{
  (e: 'node-select', run: SubagentRun | undefined): void;
}>();

const containerRef = ref<HTMLDivElement | null>(null);
const graphRef = shallowRef<Graph | null>(null);
const resizeObserverRef = shallowRef<ResizeObserver | null>(null);
const loading = ref(false);
const empty = ref(false);
const error = ref(false);

/** Internal authoritative data source: run_id → SubagentRun (accumulated incrementally via fetch + WS) */
const runStore = new Map<string, SubagentRun>();

/**
 * Request sequence number: used to discard "stale late responses".
 * loadFlow is triggered from multiple paths — onMounted / watch(currentSessionId) /
 * ws:subagents:ready / watch(colorMode), etc. Rapidly switching sessions/tabs can start several
 * concurrent async fetches; without protection, an old session's response (possibly empty)
 * would arrive late and overwrite the run tree the new session is displaying, causing
 * intermittent "no subagent runs" displays. Each loadFlow increments this number and only the
 * latest round is accepted.
 */
let loadFlowSeq = 0;

/**
 * Optional: externally specified set of runs to display (e.g. after clicking a task in the
 * sidebar to focus it, only that task's subtask subtree is shown). When this prop has a value
 * (and is non-empty), the graph renders only the runs given here and ignores the internal
 * runStore's full session tree; when unset, it falls back to rendering the complete run tree
 * from runStore (the "overall tree graph" behavior when not focused).
 */
const displayRuns = defineModel<SubagentRun[] | undefined>('displayRuns');

const colorMode = useColorMode();

/** Whether the current theme is dark */
const isDark = () => colorMode.value === 'dark';

/** Run status → node color (light theme) */
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

/** Run status → node color (dark theme) */
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

/** Run status → node color (per theme) */
const statusColor = (status: string): string => {
  return isDark() ? statusColorDark(status) : statusColorLight(status);
};

/** Run status → status text i18n key */
const statusKey = (run: SubagentRun): string => {
  const exec = run?.execution?.status;
  if (exec === 'RUNNING' || exec === 'INTERRUPTED') return 'running';
  const outcome = run?.execution?.outcome?.status;
  if (outcome === 'OK') return 'completed';
  if (outcome === 'ERROR' || outcome === 'TIMEOUT' || outcome === 'KILLED') return 'failed';
  return 'unknown';
};

/** Node main title: prefers label/task_name, then the task text */
const nodeLabel = (run: SubagentRun): string => {
  return run?.label || run?.task_name || run?.task || run?.run_id || '-';
};

/**
 * Map a list of run records into G6 graph data (tree-shaped).
 * Parent-child association (SubagentRun has no parent_run_id): when a parent run's
 * child_session_key = K, every run whose requester_session_key === K is its direct child
 * task. Edges are derived level by level from this.
 * @param runs list of run records
 * Computes the base node style for a single run (colored by run status).
 * Selection highlighting is not handled here; it is applied in place via G6's selected state
 * (see the node.state configuration in ensureGraph), avoiding a full graph rebuild / layout
 * rerun when a node is clicked.
 * @param run run record
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
 * Map a run set into G6 GraphData.
 * @param runs the run set to display
 */
const mapToGraphData = (runs: SubagentRun[]): GraphData => {
  const nodes: GraphData['nodes'] = [];
  const edges: GraphData['edges'] = [];
  const seen = new Set<string>();
  // child_session_key → run_id: used to look up the parent run from a child task's requester_session_key
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

  // Edges: parent run → child run. A child run's requester_session_key matches the parent run's
  // child_session_key; find the parent run_id via the reverse-lookup map. If not found (the
  // requester is the session root and not in the node set), skip it to avoid G6 "Node not found".
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
 * Derive GraphData from the display data source.
 * - When an external run set is specified via displayRuns (sidebar focuses a task): that task's
 *   subtree becomes the "authoritative scope", and the graph renders only those runs, avoiding
 *   drawing other unrelated sibling tasks of the same session.
 * - When displayRuns is unset: render the full run tree from runStore (the default "overall
 *   tree graph").
 * Highlighting (selected state) is managed in place by ensureGraph / applyHighlight and does
 * not participate in data derivation.
 */
const deriveDisplayData = (): GraphData => {
  const sourceRuns = displayRuns.value?.length ? displayRuns.value : Array.from(runStore.values());
  return mapToGraphData(sourceRuns);
};

/**
 * Build the G6 graph configuration and render (called only when there is no instance yet or the
 * graph structure changed substantively). Structural changes (first load / WS-introduced new
 * nodes) rebuild the data and re-run the layout; a mere selection-highlight toggle should call
 * applyHighlight() instead, avoiding a full graph rebuild and re-layout.
 */
const ensureGraph = async (data: GraphData) => {
  if (!containerRef.value) return;

  // Existing instance: the node/edge set changed wholesale (every scenario other than first
  // load: task box switching / session switching / WS increments).
  // You cannot setData brand-new nodes onto an existing instance and then re-run the d3-force
  // layout() —— on this path in G6 v5, before-layout fires but after-layout never fires, the
  // force simulation never converges, and the new nodes' coordinates stay stuck at [0,0] the
  // whole time, piling up on each other (confirmed by end-to-end reproduction).
  // Therefore destroy the instance and rebuild from the new authoritative data, ensuring
  // d3-force fully initializes every node position based on the new topology.
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
        labelText: (datum: NodeData) => {
          const label = datum['label'];
          return typeof label === 'string' ? label : '';
        },
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

  // Click a node: select it and link the detail panel below
  // In G6 v5's node:click event, evt.target has already been resolved by the forceCanvas event
  // forwarding layer's eventTargetOf() into the node element itself (its id is the run_id),
  // whether you click the node body or its label child shape. So after using targetType to
  // determine that a node was hit, simply take evt.target.id.
  graph.on(NodeEvent.CLICK, (evt: IElementEvent) => {
    // G6's node:click resolves evt.target into the node element (id is the run_id); clicking a label child shape hits the node itself too
    const id = evt.targetType === 'node' ? evt.target.id : undefined;
    if (!id) return;
    // Prefer resolving the run from the actual rendering data source: the focused subtree
    // (displayRuns) may span sessions, and its node ids may not exist in runStore, which only
    // holds the current session's descendants; hence query both.
    const run = displayRuns.value?.length ? displayRuns.value.find(r => r.run_id === id) : runStore.get(id);
    if (run) {
      selectedRunId.value = id;
      selectedRun.value = run;
      emit('node-select', run);
    }
  });

  graphRef.value = graph;
  await graph.render();
  // Sync the G6 canvas width/height when the container size changes (keep following the parent
  // container at 100%). G6 v5's no-arg resize() is not reliably supported; the container's pixel
  // size must be passed explicitly for the canvas to actually scale along.
  resizeObserverRef.value?.disconnect();
  resizeObserverRef.value = new ResizeObserver(() => {
    if (graphRef.value && containerRef.value) {
      try {
        const { clientWidth, clientHeight } = containerRef.value;
        graphRef.value.resize(clientWidth || 1, clientHeight || 1);
      } catch {
        /* canvas not ready yet, ignore */
      }
    }
  });
  if (containerRef.value) resizeObserverRef.value.observe(containerRef.value);
  // After the first render, replay the existing selection (if any) so the highlight is immediately visible
  if (selectedRunId.value) {
    try {
      await graph.setElementState(selectedRunId.value, ['selected']);
    } catch {
      /* selected node is not in the new data, ignore */
    }
  }
};

/** Destroy the current graph instance (used to release resources when switching sessions / clearing data) */
const destroyGraph = () => {
  resizeObserverRef.value?.disconnect();
  resizeObserverRef.value = null;
  graphRef.value?.destroy();
  graphRef.value = null;
};

/**
 * Selection highlight toggle: apply G6's selected state in place without destroying the
 * instance or re-running the layout. Relies on the node.state.selected style configured in
 * ensureGraph; after clicking a node, only changing that node's state makes it highlight
 * immediately.
 * @param prev the previously selected node id (highlight removed), may be empty
 * @param next the newly selected node id (lit up), may be empty
 */
const applyHighlight = async (prev?: string, next?: string) => {
  const graph = graphRef.value;
  if (!graph) return;
  if (prev && prev !== next) {
    try {
      await graph.setElementState(prev, []);
    } catch {
      /* the node may no longer be in the current display scope, ignore */
    }
  }
  if (next) {
    try {
      await graph.setElementState(next, ['selected']);
    } catch {
      /* the node may not be rendered yet, ignore (a later render will apply it based on selectedRunId) */
    }
  }
};

/** Fetch and render the subagent run tree (after writing into runStore, derive the display per the selection state) */
const loadFlow = async () => {
  const sid = currentSessionId.value;
  if (!sid) return;
  // Sequence number of this request: after an old request's await returns, if it finds that a
  // new round of loadFlow has started or the current session has switched, it discards its
  // result outright, preventing old data from overwriting the new session (root cause of the
  // intermittent empty state).
  const seq = ++loadFlowSeq;
  loading.value = true;
  error.value = false;
  empty.value = false;
  try {
    const runs = await fetchSubagentRuns(sid, 'descendants');
    // Race guard: a newer loadFlow has started or the session has changed — discard this result
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
    // The race guard applies to the error branch too: when superseded by a newer request, do not pollute the current state
    if (seq !== loadFlowSeq || sid !== currentSessionId.value) return;
    console.error('[SubagentFlowGraph] 拉取子 Agent 运行树失败：', e);
    error.value = true;
    destroyGraph();
  } finally {
    if (seq === loadFlowSeq) loading.value = false;
  }
};

/** Incremental update: merge the new event into runStore and re-render per the selection state */
const applyIncremental = (run: SubagentRun) => {
  if (!run?.run_id) return;
  if (empty.value) empty.value = false;
  if (error.value) error.value = false;

  // Write into the authoritative data source
  runStore.set(run.run_id, run);

  // Derive display data from runStore (always render the full run tree, only highlight the current selection)
  ensureGraph(deriveDisplayData());
};

/** Set up the /subagents/ws subscription (reuses the module-level singleton, no new connection) */
const setupSubagentWs = () => {
  useSubagentWs({
    onReconnect: () => {
      // After a successful reconnect the server re-sends ready, at which point a full re-fetch fills everything in
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

/** Remove the mitt subscriptions (the WS connection is a module-level singleton left for later components to reuse; not closed here) */
const teardownSubagentSubscribe = () => {
  off('ws:subagent_spawned');
  off('ws:subagent_ended');
  off('ws:subagents:ready');
};

/** Rebuild the graph on theme switch to apply the new color scheme */
watch(
  () => colorMode.value,
  () => {
    if (!empty.value && !error.value && graphRef.value) {
      loadFlow();
    }
  }
);

/** On session switch, clear the selection and re-fetch that session's run tree */
watch(
  () => currentSessionId.value,
  () => {
    selectedRunId.value = undefined;
    selectedRun.value = undefined;
    loadFlow();
  }
);

/**
 * External display scope change (sidebar focus switching: displayRuns toggles between the full
 * tree and a single task's subtree). The node/edge set and structure change accordingly, so the
 * data must be rebuilt and the layout re-run; highlighting is updated in place by applyHighlight.
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

/** When the selected run changes, only update the highlight in place (selected state); no full graph rebuild, no layout rerun */
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

