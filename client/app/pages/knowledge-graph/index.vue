<i18n lang="json">
{
  "en": {
    "knowledgeGraph": {
      "back": "Back",
      "placeholder": "Knowledge Graph feature under development…",
      "loading": "Loading…",
      "empty": "No knowledge graph data",
      "loadError": "Failed to load",
      "refresh": "Refresh",
      "upload": "Upload",
      "uploading": "Uploading…",
      "uploadAccept": "PDF, Word or text",
      "uploadSuccess": "Uploaded and graph updated",
      "uploadError": "Upload failed",
      "nodeDetailPlaceholder": "Click a node to view its details",
      "nodeType": "Type",
      "nodeContent": "Content",
      "nodeSource": "Source document",
      "nodeSourceFile": "File",
      "nodeSourceId": "Source ID",
      "nodeProperties": "Properties",
      "nodeNeighbors": "Connected nodes"
    },
    "toolbar": {
      "knowledgeGraph": "Knowledge Graph"
    }
  },
  "ja": {
    "knowledgeGraph": {
      "back": "戻る",
      "placeholder": "ナレッジグラフ機能は開発中です…",
      "loading": "読み込み中…",
      "empty": "ナレッジグラフのデータがありません",
      "loadError": "読み込みに失敗しました",
      "refresh": "更新",
      "upload": "アップロード",
      "uploading": "アップロード中…",
      "uploadAccept": "PDF・Word・テキスト",
      "uploadSuccess": "アップロードしてグラフを更新しました",
      "uploadError": "アップロードに失敗しました",
      "nodeDetailPlaceholder": "ノードをクリックすると詳細を表示します",
      "nodeType": "タイプ",
      "nodeContent": "内容",
      "nodeSource": "出典ドキュメント",
      "nodeSourceFile": "ファイル",
      "nodeSourceId": "ソースID",
      "nodeProperties": "プロパティ",
      "nodeNeighbors": "接続ノード"
    },
    "toolbar": {
      "knowledgeGraph": "ナレッジグラフ"
    }
  },
  "ko": {
    "knowledgeGraph": {
      "back": "뒤로",
      "placeholder": "지식 그래프 기능 개발 중…",
      "loading": "불러오는 중…",
      "empty": "지식 그래프 데이터가 없습니다",
      "loadError": "불러오기 실패",
      "refresh": "새로고침",
      "upload": "업로드",
      "uploading": "업로드 중…",
      "uploadAccept": "PDF·Word·텍스트",
      "uploadSuccess": "업로드 후 그래프가 갱신되었습니다",
      "uploadError": "업로드에 실패했습니다",
      "nodeDetailPlaceholder": "노드를 클릭하면 세부 정보를 표시합니다",
      "nodeType": "유형",
      "nodeContent": "내용",
      "nodeSource": "출처 문서",
      "nodeSourceFile": "파일",
      "nodeSourceId": "소스 ID",
      "nodeProperties": "속성",
      "nodeNeighbors": "연결된 노드"
    },
    "toolbar": {
      "knowledgeGraph": "지식 그래프"
    }
  },
  "zh": {
    "knowledgeGraph": {
      "back": "返回",
      "placeholder": "知识图谱功能开发中…",
      "loading": "加载中…",
      "empty": "暂无知识图谱数据",
      "loadError": "加载失败",
      "refresh": "刷新",
      "upload": "上传",
      "uploading": "上传中…",
      "uploadAccept": "PDF、Word 或文本文件",
      "uploadSuccess": "上传完成，图谱已更新",
      "uploadError": "上传失败",
      "nodeDetailPlaceholder": "点击节点查看详情",
      "nodeType": "类型",
      "nodeContent": "节点内容",
      "nodeSource": "来源文档",
      "nodeSourceFile": "文件",
      "nodeSourceId": "来源ID",
      "nodeProperties": "属性",
      "nodeNeighbors": "关联节点"
    },
    "toolbar": {
      "knowledgeGraph": "知识图谱"
    }
  }
}
</i18n>

<template>
  <div class="flex flex-col h-full bg-[#f8f9fa] dark:bg-[#131619]">
    <!-- Top title bar -->
    <div
      class="shrink-0 h-14 flex items-center gap-3 px-4 border-b border-solid border-gray-light dark:border-gray-dark bg-white dark:bg-[#1a1d21]">
      <Button
        icon="pi pi-arrow-left"
        text
        rounded
        :aria-label="t('knowledgeGraph.back')"
        @click="goBack" />
      <span class="text-base font-semibold text-gray-900 dark:text-gray-100">
        {{ t('toolbar.knowledgeGraph') }}
      </span>
      <div class="ml-auto flex items-center gap-2">
        <Button
          icon="pi pi-upload"
          :label="t('knowledgeGraph.upload')"
          text
          rounded
          :loading="uploading"
          :aria-label="t('knowledgeGraph.upload')"
          @click="triggerUpload" />
        <input
          ref="fileInputRef"
          type="file"
          multiple
          accept=".pdf,.docx,.txt,.md"
          class="hidden"
          @change="onFileSelected" />
        <Button
          icon="pi pi-refresh"
          text
          rounded
          :loading="loading"
          :aria-label="t('knowledgeGraph.refresh')"
          @click="loadGraph" />
      </div>
    </div>

    <!-- Main body: graph on top, details below (graph in the upper part, node details in the lower part) -->
    <div class="flex flex-col flex-1 min-h-0">
      <!-- Upper half: graph rendering area -->
      <div class="relative flex-[3] min-h-0 overflow-hidden">
        <div
          ref="containerRef"
          class="w-full h-full" />

        <!-- Upload result toast -->
        <div
          v-if="uploadMessage"
          class="absolute top-3 right-3 z-10 max-w-xs px-3 py-2 text-xs rounded-md shadow-md bg-white dark:bg-[#1a1d21] text-gray-700 dark:text-gray-200 border border-gray-light dark:border-gray-dark">
          <i class="pi pi-info-circle mr-1 text-blue-500" />
          {{ uploadMessage }}
        </div>

        <!-- Loading state -->
        <div
          v-if="loading"
          class="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-white/60 dark:bg-[#131619]/60">
          <i class="pi pi-spin pi-spinner text-3xl text-gray-400 dark:text-gray-500" />
          <span class="text-sm text-gray-500 dark:text-gray-400">{{ t('knowledgeGraph.loading') }}</span>
        </div>

        <!-- Empty state -->
        <div
          v-else-if="empty"
          class="absolute inset-0 flex flex-col items-center justify-center gap-4">
          <i class="pi pi-sitemap text-6xl text-gray-300 dark:text-gray-600" />
          <div class="text-base text-gray-500 dark:text-gray-400">
            {{ t('knowledgeGraph.empty') }}
          </div>
        </div>

        <!-- Error state -->
        <div
          v-else-if="error"
          class="absolute inset-0 flex flex-col items-center justify-center gap-4">
          <i class="pi pi-exclamation-triangle text-5xl text-red-400 dark:text-red-500" />
          <div class="text-base text-gray-500 dark:text-gray-400">
            {{ t('knowledgeGraph.loadError') }}
          </div>
          <Button
            :label="t('knowledgeGraph.refresh')"
            icon="pi pi-refresh"
            severity="secondary"
            @click="loadGraph" />
        </div>
      </div>

      <!-- Lower half: selected node details -->
      <div
        class="flex-[2] min-h-0 border-t border-solid border-gray-light dark:border-gray-dark bg-white/60 dark:bg-[#1a1d21]/60">
        <div
          v-if="!selectedNode"
          class="flex flex-col items-center justify-center gap-3 h-full text-center py-8">
          <i class="pi pi-hand-pointer text-3xl text-gray-300 dark:text-gray-600" />
          <span class="text-sm text-gray-500 dark:text-gray-400">{{ t('knowledgeGraph.nodeDetailPlaceholder') }}</span>
        </div>

        <div
          v-else
          class="flex flex-col gap-3 h-full min-h-0 overflow-y-auto p-4">
          <!-- Header: entity name + entity type -->
          <div
            class="rounded-lg border border-solid border-gray-light dark:border-gray-dark bg-white dark:bg-[#1a1d21] px-4 py-3 shadow-sm">
            <div class="text-sm font-medium text-gray-900 dark:text-gray-100 break-words">
              {{ selectedNode.label }}
            </div>
            <div class="text-xs text-gray-500 dark:text-gray-400 mt-1">
              {{ t('knowledgeGraph.nodeType') }}: {{ nodeTypeLabel(selectedNode) }}
            </div>
          </div>

          <!-- Node content -->
          <div
            v-if="nodeContent(selectedNode)"
            class="rounded-lg border border-solid border-gray-light dark:border-gray-dark bg-white dark:bg-[#1a1d21] px-4 py-3 shadow-sm">
            <div class="text-xs font-medium text-gray-400 dark:text-gray-500 mb-1.5">
              {{ t('knowledgeGraph.nodeContent') }}
            </div>
            <div class="text-sm text-gray-800 dark:text-gray-200 whitespace-pre-wrap break-words leading-relaxed">
              {{ nodeContent(selectedNode) }}
            </div>
          </div>

          <!-- Source document / file -->
          <div
            v-if="nodeSourceFile(selectedNode) || nodeSourceId(selectedNode)"
            class="rounded-lg border border-solid border-gray-light dark:border-gray-dark bg-white dark:bg-[#1a1d21] px-4 py-3 shadow-sm">
            <div class="text-xs font-medium text-gray-400 dark:text-gray-500 mb-1.5">
              {{ t('knowledgeGraph.nodeSource') }}
            </div>
            <div
              v-if="nodeSourceFile(selectedNode)"
              class="flex items-start gap-2 text-sm">
              <i class="pi pi-file text-gray-400 dark:text-gray-500 mt-0.5" />
              <span class="text-gray-800 dark:text-gray-200 break-all">{{ nodeSourceFile(selectedNode) }}</span>
            </div>
            <div
              v-if="nodeSourceId(selectedNode)"
              class="flex items-start gap-2 text-sm mt-1.5">
              <i class="pi pi-hashtag text-gray-400 dark:text-gray-500 mt-0.5" />
              <span class="text-gray-800 dark:text-gray-200 break-all">{{ nodeSourceId(selectedNode) }}</span>
            </div>
          </div>

          <!-- Entity properties -->
          <div
            v-if="nodeProperties(selectedNode).length > 0"
            class="rounded-lg border border-solid border-gray-light dark:border-gray-dark bg-white dark:bg-[#1a1d21] px-4 py-3 shadow-sm">
            <div class="text-xs font-medium text-gray-400 dark:text-gray-500 mb-1.5">
              {{ t('knowledgeGraph.nodeProperties') }}
            </div>
            <div class="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-2 text-sm">
              <div
                v-for="[key, value] in nodeProperties(selectedNode)"
                :key="key"
                class="flex justify-between gap-4 min-w-0">
                <span class="shrink-0 text-gray-400 dark:text-gray-500 truncate">{{ key }}</span>
                <span class="text-gray-800 dark:text-gray-200 text-right break-words">{{ formatProp(value) }}</span>
              </div>
            </div>
          </div>

          <!-- Connected nodes (neighbors) -->
          <div
            v-if="neighborsOf(selectedNode).length > 0"
            class="rounded-lg border border-solid border-gray-light dark:border-gray-dark bg-white dark:bg-[#1a1d21] px-4 py-3 shadow-sm">
            <div class="text-xs font-medium text-gray-400 dark:text-gray-500 mb-1.5">
              {{ t('knowledgeGraph.nodeNeighbors') }}
            </div>
            <div class="flex flex-wrap gap-2">
              <span
                v-for="neighbor in neighborsOf(selectedNode)"
                :key="neighbor.id"
                class="inline-flex items-center gap-1 px-2 py-1 rounded-full text-xs bg-gray-100 dark:bg-[#1f2429] text-gray-600 dark:text-gray-300 border border-solid border-gray-light dark:border-gray-dark">
                <i class="pi pi-sitemap text-[10px]" />
                {{ neighbor.label }}
              </span>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script lang="ts" setup>
// Page-level error capture: runtime errors from all descendant components on this page
// (G6 graph container / node detail panel, etc.)
// → logUtil logging + global toast; returning false stops further upward propagation
// (factory function pattern from 03-errorCapturedFactoryFunction.md)
import { useErrorCaptured } from '~/composables/errorCaptured';

useErrorCaptured();

import { ref, shallowRef, watch, onMounted, onBeforeUnmount } from 'vue';
import { useI18n } from 'vue-i18n';
import { Graph, NodeEvent } from '@antv/g6';
import type { GraphData, IElementEvent, NodeData, EdgeData } from '@antv/g6';
import { fetchApi } from '~/composables/requestApi';

const { t } = useI18n({ useScope: 'local' });
const localePath = useLocalePath();
const router = useRouter();

const goBack = () => {
  router.push(localePath('/home'));
};

/** Backend knowledge graph node */
interface BackendNode {
  id: string;
  labels: string[];
  properties: Record<string, unknown>;
}

/** Backend knowledge graph edge */
interface BackendEdge {
  id: string;
  type: string;
  source: string;
  target: string;
  properties: Record<string, unknown>;
}

/** Backend knowledge graph response */
interface KnowledgeGraphResponse {
  nodes: BackendNode[];
  edges: BackendEdge[];
  is_truncated: boolean;
}

/**
 * Mapped G6 node.
 * `data` also keeps the original labels/properties so the detail panel below can display the
 * full information of the selected node.
 */
interface GraphNode {
  id: string;
  label: string;
  data: {
    entityType: string;
    /** Original backend node (kept whenever present, for use by the detail panel) */
    raw?: BackendNode;
  };
}

/** Mapped G6 edge */
interface GraphEdge {
  id: string;
  source: string;
  target: string;
  label: string;
  data: {
    weight: number;
  };
}

/** Mapped G6 graph data */
interface MappedGraphData {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

/** Entity type → color (light theme) */
const LIGHT_PALETTE: { default: string } & Record<string, string> = {
  person: '#3b82f6',
  organization: '#f59e0b',
  location: '#10b981',
  event: '#ef4444',
  concept: '#8b5cf6',
  task: '#06b6d4',
  skill: '#ec4899',
  tool: '#84cc16',
  default: '#64748b'
};

/** Entity type → color (dark theme) */
const DARK_PALETTE: { default: string } & Record<string, string> = {
  person: '#60a5fa',
  organization: '#fbbf24',
  location: '#34d399',
  event: '#f87171',
  concept: '#a78bfa',
  task: '#22d3ee',
  skill: '#f472b6',
  tool: '#a3e635',
  default: '#94a3b8'
};

const containerRef = ref<HTMLDivElement | null>(null);
const graphRef = shallowRef<Graph | null>(null);
const loading = ref(false);
const empty = ref(false);
const error = ref(false);

/** Currently selected node id (written on node click, used for highlighting + the detail panel); undefined means nothing selected */
const selectedNodeId = ref<string | undefined>(undefined);
/** Full data of the currently selected node (including labels/properties), displayed by the detail panel below */
const selectedNode = ref<GraphNode | undefined>(undefined);
/** Full graph data (nodes + edges) from the most recent successful load, used by the detail panel to compute neighbor nodes */
const loadedGraphData = shallowRef<MappedGraphData | null>(null);

const fileInputRef = ref<HTMLInputElement | null>(null);
const uploading = ref(false);
/** Toast text after upload (success/failure); hidden when empty */
const uploadMessage = ref('');

const colorMode = useColorMode();

/** Whether the current theme is dark (defaults to dark when unknown) */
const isDark = () => colorMode.value === 'dark';

/** Pick the entity color according to the theme */
const entityColor = (entityType: string): string => {
  const palette = isDark() ? DARK_PALETTE : LIGHT_PALETTE;
  return palette[entityType] ?? palette.default;
};

/** Derive the entity type from the node's properties / labels */
const resolveEntityType = (node: BackendNode): string => {
  const fromProps = node.properties?.entity_type;
  if (typeof fromProps === 'string' && fromProps.trim().length > 0) {
    return fromProps.trim().toLowerCase();
  }
  const firstLabel = node.labels?.[0];
  if (typeof firstLabel === 'string' && firstLabel.trim().length > 0) {
    return firstLabel.trim().toLowerCase();
  }
  return 'default';
};

/** Backend data → G6 graph data */
const mapGraphData = (payload: KnowledgeGraphResponse): MappedGraphData => {
  const nodes: GraphNode[] = (payload.nodes ?? []).map(node => ({
    id: node.id,
    label: typeof node.properties?.name === 'string' ? node.properties.name : node.id,
    data: {
      entityType: resolveEntityType(node),
      raw: node
    }
  }));

  const edges: GraphEdge[] = (payload.edges ?? [])
    .filter(edge => edge.source && edge.target)
    .map(edge => ({
      id: edge.id,
      source: edge.source,
      target: edge.target,
      label: edge.type,
      data: {
        weight: typeof edge.properties?.weight === 'number' ? edge.properties.weight : 1
      }
    }));

  return { nodes, edges };
};

/** Detail panel: display the entity type label (falls back to default when unknown) */
const nodeTypeLabel = (node: GraphNode): string => {
  const entityType = node.data.entityType || 'default';
  const palette = isDark() ? DARK_PALETTE : LIGHT_PALETTE;
  return palette[entityType] !== undefined ? entityType : 'default';
};

/** Detail panel: node properties (name is already shown as the title, so it is excluded to avoid duplication) */
const nodeProperties = (node: GraphNode): Array<[string, unknown]> => {
  const raw = node.data.raw;
  if (!raw?.properties) return [];
  return Object.entries(raw.properties).filter(([key]) => {
    return ![
      'name',
      'entity_id',
      'entity_type',
      'entity_name',
      'description',
      'source_id',
      'file_path',
      'created_at'
    ].includes(key);
  });
};

/** Detail panel: node content (the LightRAG entity summary, the field closest to a "body text"; falls back to description when content is absent) */
const nodeContent = (node: GraphNode): string => {
  const props = node.data.raw?.properties;
  if (!props) return '';
  const content = props['content'];
  if (typeof content === 'string' && content.trim().length > 0) return content.trim();
  const description = props['description'];
  if (typeof description === 'string' && description.trim().length > 0) return description.trim();
  return '';
};

/** Detail panel: source file path */
const nodeSourceFile = (node: GraphNode): string => {
  const props = node.data.raw?.properties;
  if (!props) return '';
  const filePath = props['file_path'];
  if (typeof filePath === 'string' && filePath.trim().length > 0) return filePath.trim();
  return '';
};

/** Detail panel: source document ID */
const nodeSourceId = (node: GraphNode): string => {
  const props = node.data.raw?.properties;
  if (!props) return '';
  const sourceId = props['source_id'];
  if (typeof sourceId === 'string' && sourceId.trim().length > 0) return sourceId.trim();
  return '';
};

/** Detail panel: property value formatting (objects/arrays → JSON string) */
const formatProp = (value: unknown): string => {
  if (value === null || value === undefined) return '';
  if (typeof value === 'object') {
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }
  return String(value);
};

/** Detail panel: neighbor nodes directly connected to the selected node (used to display related entities) */
const neighborsOf = (node: GraphNode): GraphNode[] => {
  const data = loadedGraphData.value;
  if (!data) return [];
  const neighborIds = new Set<string>();
  for (const edge of data.edges) {
    if (edge.source === node.id) neighborIds.add(edge.target);
    if (edge.target === node.id) neighborIds.add(edge.source);
  }
  return data.nodes.filter(n => n.id !== node.id && neighborIds.has(n.id));
};

/** Build the G6 graph config and render it */
const renderGraph = async (data: MappedGraphData) => {
  if (!containerRef.value) return;

  graphRef.value?.destroy();
  graphRef.value = null;

  const dark = isDark();
  const background = dark ? '#131619' : '#f8f9fa';
  const nodeFill = dark ? '#1a1d21' : '#ffffff';
  const labelFill = dark ? '#e5e7eb' : '#1f2937';
  const edgeStroke = dark ? '#4b5563' : '#9ca3af';
  const edgeLabelFill = dark ? '#9ca3af' : '#6b7280';

  const graphData: GraphData = {
    nodes: data.nodes.map(node => ({
      id: node.id,
      label: node.label,
      data: node.data,
      style: {
        fill: nodeFill,
        stroke: entityColor(node.data.entityType),
        lineWidth: 2,
        size: 28
      }
    })),
    edges: data.edges.map(edge => ({
      id: edge.id,
      source: edge.source,
      target: edge.target,
      label: edge.label,
      data: edge.data,
      style: {
        stroke: edgeStroke,
        lineWidth: Math.min(1 + edge.data.weight, 4),
        labelText: edge.label,
        labelFill: edgeLabelFill,
        labelFontSize: 10,
        endArrow: true
      }
    }))
  };

  const graph = new Graph({
    container: containerRef.value,
    data: graphData,
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
        labelFontSize: 12,
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
        stroke: edgeStroke,
        lineWidth: 1.5,
        labelText: (datum: EdgeData) => {
          const label = datum['label'];
          return typeof label === 'string' ? label : '';
        },
        labelFill: edgeLabelFill,
        labelFontSize: 10,
        endArrow: true
      }
    },
    behaviors: ['drag-canvas', 'zoom-canvas', 'drag-element'],
    layout: {
      type: 'd3-force',
      collide: {
        radius: 40
      },
      link: {
        distance: 120
      },
      manyBody: {
        strength: -200
      }
    }
  });

  // Node click: select the node and sync the detail panel below.
  // In G6 v5's node:click event, evt.target has already been resolved by the event forwarding
  // layer into the node element itself (its id is the node id) — the same whether the node body
  // or one of its label child shapes was hit. After confirming the hit is a node via targetType,
  // simply take evt.target.id.
  graph.on(NodeEvent.CLICK, (evt: IElementEvent) => {
    const id = evt.targetType === 'node' ? evt.target.id : undefined;
    if (!id) return;
    const node = data.nodes.find(n => n.id === id);
    if (node) {
      selectedNodeId.value = id;
      selectedNode.value = node;
    }
  });

  graphRef.value = graph;
  await graph.render();

  // After the first render, replay any existing selection (if present) so the highlight is
  // immediately visible.
  // By the time G6 render() completes, the nodes are ready, so we can light it up synchronously
  // here.
  if (selectedNodeId.value) {
    try {
      graph.setElementState(selectedNodeId.value, ['selected']);
    } catch {
      /* The selected node is not in the new data; ignore */
    }
  }
};

/**
 * Selection highlight switching: applies G6's selected state in place, without destroying the
 * instance or re-running the layout.
 * Relies on the node.state.selected style above; after a node is clicked, only that node's state
 * needs to change for an instant highlight.
 * @param prev The previously selected node id (to un-highlight); may be empty
 * @param next The newly selected node id (to light up); may be empty
 */
const applyHighlight = async (prev?: string, next?: string) => {
  const graph = graphRef.value;
  if (!graph) return;
  if (prev && prev !== next) {
    try {
      await graph.setElementState(prev, []);
    } catch {
      /* The node may no longer be within the currently displayed range; ignore */
    }
  }
  if (next) {
    try {
      await graph.setElementState(next, ['selected']);
    } catch {
      /* The node may not be rendered yet; ignore (a later render() will restore it based on selectedNodeId) */
    }
  }
};

/** File extensions allowed for upload (kept in sync with the backend's _ALLOWED_EXT) */
const ALLOWED_EXTENSIONS = ['.pdf', '.docx', '.txt', '.md'];

/** Check whether a single file's extension is allowed */
const isAllowedFile = (name: string): boolean => {
  const ext = name.slice(name.lastIndexOf('.')).toLowerCase();
  return ALLOWED_EXTENSIONS.includes(ext);
};

/** Upload button click → open the file picker */
const triggerUpload = () => {
  fileInputRef.value?.click();
};

/** Trigger the upload after files are selected */
const onFileSelected = (event: Event) => {
  const input = event.target as HTMLInputElement;
  const files = Array.from(input.files ?? []);
  input.value = '';
  if (files.length === 0) return;
  uploadFiles(files);
};

/** Upload files to the backend and reload the graph */
const uploadFiles = async (files: File[]) => {
  if (uploading.value) return;

  const invalid = files.find(f => !isAllowedFile(f.name));
  if (invalid) {
    uploadMessage.value = t('knowledgeGraph.uploadError');
    return;
  }

  uploading.value = true;
  uploadMessage.value = '';
  try {
    const formData = new FormData();
    for (const file of files) {
      formData.append('files', file);
    }
    const payload = (await fetchApi({
      url: '/knowledge-graph/upload',
      method: 'post',
      contentType: 'multipart/form-data',
      opts: formData
    })) as unknown as { success?: boolean; message?: string };

    if (payload?.success) {
      uploadMessage.value = t('knowledgeGraph.uploadSuccess');
      loadGraph();
    } else {
      uploadMessage.value = payload?.message ?? t('knowledgeGraph.uploadError');
    }
  } catch (e) {
    console.error('[KnowledgeGraph] Failed to upload:', e);
    uploadMessage.value = t('knowledgeGraph.uploadError');
  } finally {
    uploading.value = false;
  }
};

/** Fetch and render the knowledge graph */
const loadGraph = async () => {
  loading.value = true;
  error.value = false;
  empty.value = false;
  try {
    const payload = (await fetchApi({ url: '/knowledge-graph', method: 'get' })) as unknown;
    const response = payload as KnowledgeGraphResponse;
    const nodes = Array.isArray(response?.nodes) ? response.nodes : [];
    if (nodes.length === 0) {
      empty.value = true;
      graphRef.value?.destroy();
      graphRef.value = null;
      loadedGraphData.value = null;
      selectedNodeId.value = undefined;
      selectedNode.value = undefined;
      return;
    }
    const mapped = mapGraphData(response);
    loadedGraphData.value = mapped;
    renderGraph(mapped);
  } catch (e) {
    console.error('[KnowledgeGraph] Failed to load graph:', e);
    error.value = true;
    graphRef.value?.destroy();
    graphRef.value = null;
    loadedGraphData.value = null;
    selectedNodeId.value = undefined;
    selectedNode.value = undefined;
  } finally {
    loading.value = false;
  }
};

/** Rebuild the graph when the theme changes to apply the new color scheme */
watch(
  () => colorMode.value,
  () => {
    if (!empty.value && !error.value && graphRef.value) {
      loadGraph();
    }
  }
);

/** When the selected node changes, update only the highlight in place (selected state); no full graph rebuild, no re-layout */
watch(
  () => selectedNodeId.value,
  (next, prev) => {
    if (!empty.value && !error.value && graphRef.value) {
      applyHighlight(prev, next);
    }
  }
);

onMounted(() => {
  loadGraph();
});

onBeforeUnmount(() => {
  graphRef.value?.destroy();
  graphRef.value = null;
});
</script>
