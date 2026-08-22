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
      "uploadError": "Upload failed"
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
      "uploadError": "アップロードに失敗しました"
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
      "uploadError": "업로드에 실패했습니다"
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
      "uploadError": "上传失败"
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

    <!-- 图谱渲染区 -->
    <div class="relative flex-1 w-full h-full overflow-hidden">
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
  </div>
</template>

<script lang="ts" setup>
import { ref, shallowRef, watch, onMounted, onBeforeUnmount } from 'vue';
import { useI18n } from 'vue-i18n';
import { Graph } from '@antv/g6';
import type { GraphData } from '@antv/g6';
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

/** 映射后的 G6 节点 */
interface GraphNode {
  id: string;
  label: string;
  data: {
    entityType: string;
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
      entityType: resolveEntityType(node)
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

/** 构建 G6 图配置并渲染 */
const renderGraph = (data: MappedGraphData) => {
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

  graphRef.value = graph;
  graph.render();
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
      return;
    }
    renderGraph(mapGraphData(response));
  } catch (e) {
    console.error('[KnowledgeGraph] Failed to load graph:', e);
    error.value = true;
    graphRef.value?.destroy();
    graphRef.value = null;
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

onMounted(() => {
  loadGraph();
});

onBeforeUnmount(() => {
  graphRef.value?.destroy();
  graphRef.value = null;
});
</script>
