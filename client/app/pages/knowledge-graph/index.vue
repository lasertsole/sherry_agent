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
    <!-- 顶部标题栏 -->
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

    <!-- 主体：上树下详（图谱在上，节点详情在下） -->
    <div class="flex flex-col flex-1 min-h-0">
      <!-- 上半：图谱渲染区 -->
      <div class="relative flex-[3] min-h-0 overflow-hidden">
        <div ref="containerRef" class="w-full h-full" />

        <!-- 上传结果提示 -->
        <div
          v-if="uploadMessage"
          class="absolute top-3 right-3 z-10 max-w-xs px-3 py-2 text-xs rounded-md shadow-md
                 bg-white dark:bg-[#1a1d21] text-gray-700 dark:text-gray-200
                 border border-gray-light dark:border-gray-dark">
          <i class="pi pi-info-circle mr-1 text-blue-500" />
          {{ uploadMessage }}
        </div>

        <!-- 加载中 -->
        <div
          v-if="loading"
          class="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-white/60 dark:bg-[#131619]/60">
          <i class="pi pi-spin pi-spinner text-3xl text-gray-400 dark:text-gray-500" />
          <span class="text-sm text-gray-500 dark:text-gray-400">{{ t('knowledgeGraph.loading') }}</span>
        </div>

        <!-- 空态 -->
        <div
          v-else-if="empty"
          class="absolute inset-0 flex flex-col items-center justify-center gap-4">
          <i class="pi pi-sitemap text-6xl text-gray-300 dark:text-gray-600" />
          <div class="text-base text-gray-500 dark:text-gray-400">
            {{ t('knowledgeGraph.empty') }}
          </div>
        </div>

        <!-- 错误态 -->
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

      <!-- 下半：选中节点详情 -->
      <div
        class="flex-[2] min-h-0 border-t border-solid border-gray-light dark:border-gray-dark bg-white/60 dark:bg-[#1a1d21]/60">
        <div v-if="!selectedNode" class="flex flex-col items-center justify-center gap-3 h-full text-center py-8">
          <i class="pi pi-hand-pointer text-3xl text-gray-300 dark:text-gray-600" />
          <span class="text-sm text-gray-500 dark:text-gray-400">{{ t('knowledgeGraph.nodeDetailPlaceholder') }}</span>
        </div>

        <div v-else class="flex flex-col gap-3 h-full min-h-0 overflow-y-auto p-4">
          <!-- 头部：实体名 + 实体类型 -->
          <div
            class="rounded-lg border border-solid border-gray-light dark:border-gray-dark bg-white dark:bg-[#1a1d21] px-4 py-3 shadow-sm">
            <div class="text-sm font-medium text-gray-900 dark:text-gray-100 break-words">
              {{ selectedNode.label }}
            </div>
            <div class="text-xs text-gray-500 dark:text-gray-400 mt-1">
              {{ t('knowledgeGraph.nodeType') }}: {{ nodeTypeLabel(selectedNode) }}
            </div>
          </div>

          <!-- 节点内容 -->
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

          <!-- 来源文档 / 文件 -->
          <div
            v-if="nodeSourceFile(selectedNode) || nodeSourceId(selectedNode)"
            class="rounded-lg border border-solid border-gray-light dark:border-gray-dark bg-white dark:bg-[#1a1d21] px-4 py-3 shadow-sm">
            <div class="text-xs font-medium text-gray-400 dark:text-gray-500 mb-1.5">
              {{ t('knowledgeGraph.nodeSource') }}
            </div>
            <div v-if="nodeSourceFile(selectedNode)" class="flex items-start gap-2 text-sm">
              <i class="pi pi-file text-gray-400 dark:text-gray-500 mt-0.5" />
              <span class="text-gray-800 dark:text-gray-200 break-all">{{ nodeSourceFile(selectedNode) }}</span>
            </div>
            <div v-if="nodeSourceId(selectedNode)" class="flex items-start gap-2 text-sm mt-1.5">
              <i class="pi pi-hashtag text-gray-400 dark:text-gray-500 mt-0.5" />
              <span class="text-gray-800 dark:text-gray-200 break-all">{{ nodeSourceId(selectedNode) }}</span>
            </div>
          </div>

          <!-- 实体属性 -->
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

          <!-- 关联的节点（邻居） -->
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
// 页面级错误捕获：本页所有后代组件（G6 图谱容器/节点详情面板等）的运行时错误
// → logUtil 日志 + 全局 toast，return false 阻断向上冒泡
// （03-errorCaptured工厂函数.md 工厂函数模式）
import { useErrorCaptured } from '~/composables/errorCaptured';

useErrorCaptured();

import { ref, shallowRef, watch, onMounted, onBeforeUnmount } from 'vue';
import { useI18n } from 'vue-i18n';
import { Graph, NodeEvent } from '@antv/g6';
import type { GraphData, IElementEvent } from '@antv/g6';
import { fetchApi } from '~/composables/requestApi';

const { t } = useI18n({ useScope: 'local' });
const localePath = useLocalePath();
const router = useRouter();

const goBack = () => {
  router.push(localePath('/home'));
};

/** 后端知识图谱节点 */
interface BackendNode {
  id: string;
  labels: string[];
  properties: Record<string, unknown>;
}

/** 后端知识图谱边 */
interface BackendEdge {
  id: string;
  type: string;
  source: string;
  target: string;
  properties: Record<string, unknown>;
}

/** 后端知识图谱响应 */
interface KnowledgeGraphResponse {
  nodes: BackendNode[];
  edges: BackendEdge[];
  is_truncated: boolean;
}

/**
 * 映射后的 G6 节点。
 * data 内同时保留原始 labels/properties，供下方详情面板展示选中节点的完整信息。
 */
interface GraphNode {
  id: string;
  label: string;
  data: {
    entityType: string;
    /** 原始后端节点（非空即保留，供详情面板使用） */
    raw?: BackendNode;
  };
}

/** 映射后的 G6 边 */
interface GraphEdge {
  id: string;
  source: string;
  target: string;
  label: string;
  data: {
    weight: number;
  };
}

/** 映射后的 G6 图数据 */
interface MappedGraphData {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

/** 实体类型 → 颜色（浅色主题） */
const LIGHT_PALETTE: Record<string, string> = {
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

/** 实体类型 → 颜色（深色主题） */
const DARK_PALETTE: Record<string, string> = {
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

/** 当前选中的节点 id（点击节点时写入，用于高亮 + 详情面板）；undefined 表示未选中 */
const selectedNodeId = ref<string | undefined>(undefined);
/** 当前选中的完整节点数据（含 labels/properties），供下方详情面板展示 */
const selectedNode = ref<GraphNode | undefined>(undefined);
/** 最近一次成功加载的完整图数据（nodes + edges），供详情面板计算邻居节点 */
const loadedGraphData = shallowRef<MappedGraphData | null>(null);

const fileInputRef = ref<HTMLInputElement | null>(null);
const uploading = ref(false);
/** 上传后的提示文本（成功/失败），为空时不展示 */
const uploadMessage = ref('');

const colorMode = useColorMode();

/** 当前是否为深色主题（未知时默认深色） */
const isDark = () => colorMode.value === 'dark';

/** 根据主题取实体颜色 */
const entityColor = (entityType: string): string => {
  const palette = isDark() ? DARK_PALETTE : LIGHT_PALETTE;
  return palette[entityType] ?? palette.default;
};

/** 从节点 properties / labels 推导实体类型 */
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

/** 后端数据 → G6 图数据 */
const mapGraphData = (payload: KnowledgeGraphResponse): MappedGraphData => {
  const nodes: GraphNode[] = (payload.nodes ?? []).map(node => ({
    id: node.id,
    label: node.properties?.name ?? node.id,
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

/** 详情面板：展示实体类型标签（默认降级为 default） */
const nodeTypeLabel = (node: GraphNode): string => {
  const entityType = node.data.entityType || 'default';
  const palette = isDark() ? DARK_PALETTE : LIGHT_PALETTE;
  return palette[entityType] !== undefined ? entityType : 'default';
};

/** 详情面板：节点属性（name 已作为标题展示，故排除，避免重复） */
const nodeProperties = (node: GraphNode): Array<[string, unknown]> => {
  const raw = node.data.raw;
  if (!raw?.properties) return [];
  return Object.entries(raw.properties).filter(([key]) => {
    return !['name', 'entity_id', 'entity_type', 'entity_name', 'description', 'source_id', 'file_path', 'created_at'].includes(key);
  });
};

/** 详情面板：节点内容（LightRAG 实体摘要，最接近「正文」的字段；无 content 时回退 description） */
const nodeContent = (node: GraphNode): string => {
  const props = node.data.raw?.properties;
  if (!props) return '';
  const content = props['content'];
  if (typeof content === 'string' && content.trim().length > 0) return content.trim();
  const description = props['description'];
  if (typeof description === 'string' && description.trim().length > 0) return description.trim();
  return '';
};

/** 详情面板：来源文件路径 */
const nodeSourceFile = (node: GraphNode): string => {
  const props = node.data.raw?.properties;
  if (!props) return '';
  const filePath = props['file_path'];
  if (typeof filePath === 'string' && filePath.trim().length > 0) return filePath.trim();
  return '';
};

/** 详情面板：来源文档 ID */
const nodeSourceId = (node: GraphNode): string => {
  const props = node.data.raw?.properties;
  if (!props) return '';
  const sourceId = props['source_id'];
  if (typeof sourceId === 'string' && sourceId.trim().length > 0) return sourceId.trim();
  return '';
};

/** 详情面板：属性值格式化（对象/数组 → JSON 字符串） */
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

/** 详情面板：与选中节点直接相连的邻居节点（用于展示关联实体） */
const neighborsOf = (node: GraphNode): GraphNode[] => {
  const data = loadedGraphData.value;
  if (!data) return [];
  const neighborIds = new Set<string>();
  for (const edge of data.edges) {
    if (edge.source === node.id) neighborIds.add(edge.target);
    if (edge.target === node.id) neighborIds.add(edge.source);
  }
  return data.nodes.filter((n) => n.id !== node.id && neighborIds.has(n.id));
};

/** 构建 G6 图配置并渲染 */
const renderGraph = async (data: MappedGraphData) => {
  if (!containerRef.value) return;

  graphRef.value?.destroy();
  graphRef.value = null;

  const dark = isDark();
  const background = dark ? '#131619' : '#f8f9fa';
  const nodeFill = dark ? '#1a1d21' : '#ffffff';
  const nodeStroke = dark ? '#3f4650' : '#d1d5db';
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
        labelText: (datum: { label?: string }) => datum.label ?? '',
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
        labelText: (datum: { label?: string }) => datum.label ?? '',
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

  // 点击节点：选中并联动下方详情面板。
  // G6 v5 的 node:click 事件中 evt.target 已被事件转发层解析为节点元素本身（其 id 即节点 id），
  // 无论点中节点主体还是其 label 子形状都一样，用 targetType 判定命中节点后取 evt.target.id 即可。
  graph.on(NodeEvent.CLICK, (evt: IElementEvent) => {
    const id = evt.targetType === 'node' ? evt.target.id : undefined;
    if (!id) return;
    const node = data.nodes.find((n) => n.id === id);
    if (node) {
      selectedNodeId.value = id;
      selectedNode.value = node;
    }
  });

  graphRef.value = graph;
  await graph.render();

  // 首次渲染后重放既有选中（若存在），使高亮即时可见。
  // G6 render() 完成后节点已就绪，这里再同步点亮即可。
  if (selectedNodeId.value) {
    try {
      graph.setElementState(selectedNodeId.value, ['selected']);
    } catch {
      /* 选中节点不在新数据内，忽略 */
    }
  }
};

/**
 * 选中高亮切换：就地应用 G6 的 selected state，不销毁实例、不重跑布局。
 * 依赖上面 node.state.selected 样式，点击节点后仅改变该节点状态即可即时高亮。
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
      /* 节点可能尚未渲染，忽略（后续 render 时会据 selectedNodeId 补齐） */
    }
  }
};

/** 允许上传的文件后缀（与后端 _ALLOWED_EXT 保持一致） */
const ALLOWED_EXTENSIONS = ['.pdf', '.docx', '.txt', '.md'];

/** 校验单个文件后缀是否被允许 */
const isAllowedFile = (name: string): boolean => {
  const ext = name.slice(name.lastIndexOf('.')).toLowerCase();
  return ALLOWED_EXTENSIONS.includes(ext);
};

/** 点击上传按钮 → 弹出文件选择框 */
const triggerUpload = () => {
  fileInputRef.value?.click();
};

/** 文件选择后触发上传 */
const onFileSelected = (event: Event) => {
  const input = event.target as HTMLInputElement;
  const files = Array.from(input.files ?? []);
  input.value = '';
  if (files.length === 0) return;
  uploadFiles(files);
};

/** 上传文件到后端并重新加载图谱 */
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

/** 拉取并渲染知识图谱 */
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

/** 主题切换时重建图谱以应用新配色 */
watch(
  () => colorMode.value,
  () => {
    if (!empty.value && !error.value && graphRef.value) {
      loadGraph();
    }
  }
);

/** 选中节点变化时只就地更新高亮（selected state），不重建整图、不重排布局 */
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
