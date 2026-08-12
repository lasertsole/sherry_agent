<template>
  <div class="w-full h-full flex text-theme-main">
    <!-- 隐藏的图片文件选择框（由工具栏「图片」按钮触发） -->
    <input
      ref="imageFileInputRef"
      type="file"
      accept="image/*"
      multiple
      class="hidden"
      @change="onImageSelected" />
    <!-- 移动端菜单遮罩层 -->
    <div
      v-if="isSidebarOpen"
      class="fixed inset-0 bg-[#ddd] opacity-30 z-40 md:hidden"
      @click="isSidebarOpen = false"></div>

    <!-- 左侧-历史记录区域 -->
    <!-- 移动端：固定定位，默认隐藏，通过按钮切换 -->
    <!-- md：固定定位，显示宽度 280px -->
    <!-- lg：相对定位，显示宽度 360px -->
    <div
      :class="[
        'flex flex-col px-4 fixed md:relative h-full md:h-auto md:translate-x-0',
        'transition-transform duration-300 z-50 md:z-auto w-[280px] md:w-[280px] lg:w-[360px]',
        'border-r border-solid border-gray-light bg-[#fff] dark:border-gray-dark dark:bg-[#2a2a36]',
        isSidebarOpen ? 'translate-x-0' : '-translate-x-full'
      ]">
      <!-- LOGO区域 -->
      <div class="flex items-center h-15 text-xl">🍊{{ characterInfo.aiName }}</div>
      <!-- 记录列表 -->
      <div class="flex flex-col overflow-auto flex-1 gap-3">
        <div
          v-if="historyList.length === 0"
          class="flex items-center justify-center h-full w-full text-[#868686]">
          暂无会话记录
        </div>
        <HistoryItem
          v-for="(item, index) in historyList"
          :key="item.id"
          :history-record="item"
          :is-active="currentSessionId === item.id"
          @choose-session="handleToggleSession"
          v-model:selectedList="selectedSessionIds" />
      </div>
      <div class="h-17 flex items-center justify-between">
        <div class="flex items-center justify-center gap-1">
          <Checkbox
            v-model="isCheckAllSession"
            :indeterminate="isIndeterminate"
            binary />
          <span>全选</span>
        </div>
        <Button
          icon="pi pi-trash"
          label="批量删除对话" />
      </div>
    </div>

    <!-- 右侧-会话主体区域 -->
    <div class="flex flex-col flex-1 h-full bg-white dark:bg-[#131619]">
      <!-- 顶部工具栏 -->
      <div
        class="flex md:justify-end justify-between box-border border-b border-solid border-gray-light dark:border-gray-dark p-3 h-15">
        <!-- 移动端菜单切换按钮 -->
        <Button
          icon="pi pi-bars"
          class="md:hidden"
          variant="text"
          @click="isSidebarOpen = !isSidebarOpen" />
        <!-- 移动端展示 -->
        <div class="md:hidden h-full flex items-center text-xl">🍊{{ characterInfo.aiName }}</div>
        <!-- 顶部工具栏 -->
        <div class="flex items-center gap-3">
          <ModeSwitch />
          <Button
            icon="pi pi-cog"
            class="md:hidden"
            @click="openHeaderMenu"
            variant="text"
            type="button"
            aria-haspopup="true"
            aria-controls="header_tools" />
          <Menu
            class="md:hidden"
            ref="headerToolsMenuRef"
            id="header_tools"
            :model="headerTools"
            :popup="true"></Menu>
          <div class="hidden md:flex justify-end items-center flex-1 gap-3">
            <Button
              :icon="tool.icon"
              v-for="tool in headerTools"
              :key="tool.event"
              :title="tool.title"
              @click="handleOperate('headerBar', tool.event)"
              :label="tool.toolName"
              variant="text" />
          </div>
        </div>
      </div>
      <!-- 聊天主体 -->
      <ChatBox
        :messages="chatMessages"
        :user-avatar="characterInfo.userAvatar"
        :ai-avatar="characterInfo.aiAvatar"
        :user-name="characterInfo.userName"
        :ai-name="characterInfo.aiName" />
      <!-- 图片预览区（独立于输入框上方，避免挤压 h-40 输入框导致发送按钮上移 / ✕ 按钮被裁剪） -->
      <template v-if="selectedImages.length > 0">
        <div class="flex items-center gap-2 px-2 py-2 border-t border-solid border-gray-light dark:border-gray-dark overflow-x-auto">
          <div
            v-for="(img, idx) in selectedImages"
            :key="idx"
            class="relative shrink-0 group">
            <img
              :src="`data:image/*;base64,${img.base64}`"
              :alt="img.name"
              class="w-16 h-16 object-cover rounded-lg border border-solid border-gray-light dark:border-gray-dark cursor-pointer hover:opacity-80 transition-opacity duration-200"
              @click="openPreview(`data:image/*;base64,${img.base64}`)" />
            <button
              type="button"
              :title="'移除图片'"
              class="absolute top-0.5 right-0.5 z-10 w-6 h-6 flex items-center justify-center rounded-full bg-[#ef4444] text-white text-sm leading-none shadow-md cursor-pointer"
              @click="removeImage(idx)">
              ✕
            </button>
          </div>
        </div>
      </template>
      <!-- 聊天输入框区域（固定 h-40，发送按钮位置稳定） -->
      <div class="flex flex-col h-40">
        <!-- 聊天工具 -->
        <div class="h-8 px-2 flex items-center gap-3 border-b border-solid border-gray-light dark:border-gray-dark">
          <template class="hidden sm:block">
            <Button
              v-for="tool in tools"
              :key="tool.event"
              :icon="tool.icon"
              :label="tool.toolName"
              @click="handleOperate('toolBar', tool.event)"
              size="small"
              variant="text" />
          </template>
          <template class="block sm:hidden">
            <Button
              v-for="tool in tools"
              :key="tool.event"
              :icon="tool.icon"
              :aria-label="tool.toolName"
              @click="handleOperate('toolBar', tool.event)"
              size="small"
              variant="text" />
          </template>
        </div>
        <!-- 输入框 -->
        <ChatInputBox :sending="isSending" @send="handleSend" @stop="handleStop" />
      </div>
    </div>
  </div>
</template>

<script lang="ts" setup>
// components
import HistoryItem from './components/HistoryItem.vue';
import ModeSwitch from './components/ModeSwitch.vue';
import ChatBox from './components/ChatBox.vue';
// function
import { computed, onMounted } from 'vue';
import type { SessionRecord, MessageItem } from './type.ts';
import { CHAT_ROLE } from './type.ts';
import type { CachedMessage } from '@/composables/db';
import { tools, headerTools } from './config';
import { Menu } from 'primevue';
import { readCharacter } from '@/composables/bridge';
import type { ChatRequest } from '@/composables/bridge';
import type { AgentChunkType } from '@/composables/bridge';

// 图片预览
const { openPreview } = useImagePreview();

/** 侧边栏展开状态（移动端） */
const isSidebarOpen = ref(false);

/**
 * 角色显示信息（来自服务端 character.json）
 * - user.avatar / assistant.avatar 为相对 `static/` 的路径，拼接 base URL 得到完整图片地址
 */
const backendBaseUrl = ref<string>(import.meta.env.VITE_API_BACK_URL || 'http://localhost:8080');
const characterInfo = ref<{ userName: string; userAvatar: string; aiName: string; aiAvatar: string }>({
  userName: '我',
  userAvatar: '',
  aiName: '橘雪莉',
  aiAvatar: ''
});

/** 解析角色配置：把 `avatar/xxx.jpg` 形式的路径拼接成完整静态资源 URL */
const resolveCharacter = (data?: Record<string, Record<string, string>>) => {
  const base = (backendBaseUrl.value || '').replace(/\/+$/, '');
  const user = data?.user ?? {};
  const assistant = data?.assistant ?? {};
  const resolveUrl = (path?: string) => (path ? `${base}/static/${path.replace(/^\/+/, '')}` : '');
  characterInfo.value = {
    userName: user.name || '我',
    userAvatar: resolveUrl(user.avatar),
    aiName: assistant.name || 'AI',
    aiAvatar: resolveUrl(assistant.avatar)
  };
};

/** 从服务端加载角色配置（头像 + 名字） */
const loadCharacter = async () => {
  try {
    const data = (await readCharacter()) as Record<string, Record<string, string>>;
    resolveCharacter(data);
  } catch (error) {
    // 服务端不可达时保留默认头像与名字
    console.warn('[loadCharacter] 获取角色配置失败，保留默认头像：', error);
  }
};

/** 历史会话 */
const historyList = ref<SessionRecord[]>([
  {
    id: '1',
    title: '示例会话',
    createTime: '2026-06-17 10:42'
  }
]);

/** 当前会话 */
const currentSession = ref<SessionRecord>();
const currentSessionId = ref<string>();

/**
 * 当前会话要渲染的消息列表 —— 单一数据源。
 *
 * 历史上直接对 `currentSession.value` 做整体赋值（`= {...}`），导致
 * 「loadSessionHistory 的迟到结果覆盖用户刚发送的本地消息」的竞态，表现为
 * 发送后列表被清空。现在所有追加/合并都只操作这个数组，不再整体重建会话对象。
 */
const chatMessages = ref<MessageItem[]>([]);

/**
 * 将后端返回的 content 归一化为纯文本字符串。
 *
 * 后端 messages 表的 content 存在两种形态：
 * 1. 多模态结构化数组：`[{ type: 'text', text: '...' }, { type: 'image', ... }]`
 * 2. 纯文本字符串：`'...'`
 *
 * ChatBox 通过 markdown-it 渲染 content，其只接受字符串（传入数组会抛
 * `Error: Input data should be a String`，导致整个消息列表渲染中断）。
 * 这里把数组形式拆解为纯文本字符串（丢弃非文本的分段，仅拼接 text 字段），
 * 保证渲染安全且内容连续。
 */
const normalizeContent = (content: unknown): string => {
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) {
    return content
      .map((part: unknown) =>
        typeof part === 'object' && part !== null && typeof (part as { text?: unknown }).text === 'string'
          ? (part as { text: string }).text
          : '',
      )
      .join('');
  }
  // 其它形态（null / 数字 / 对象等）统一兜底为空字符串
  return '';
};

/**
 * 将后端返回的历史消息行（CachedMessage[]）转为聊天列表所需的 MessageItem[]。
 * 结构与后端 messages 表一致，仅对可能为空的字段做兜底，保证 ChatBox 渲染安全。
 */
const toMessageItems = (rows: CachedMessage[]): MessageItem[] =>
  rows.map(row => ({
    session_id: row.session_id,
    role: row.role as CHAT_ROLE,
    content: normalizeContent(row.content),
    // 透传图片数组：用户消息为 base64，AI 消息为持久化文件路径，交由 ChatBox 区分渲染
    images: row.images ?? undefined,
    id: row.id,
    turn_num: row.turn_num,
    timestamp: row.timestamp ?? ''
  }));

/**
 * 加载指定会话的历史消息（本地缓存优先，后台合并服务端增量），
 * 合并进 `chatMessages`（按 id 去重），供 ChatBox 渲染。
 *
 * 修复：不再整体重建 `currentSession.value`（那会覆盖用户已发送的本地消息，
 * 导致「发送后列表被清空」）。只把历史行合并进单一列表，已存在的消息保留。
 */
const loadSessionHistory = async (sessionId: string) => {
  currentSessionId.value = sessionId;

  const rows = await get_history_by_turn_page(sessionId, 0, 10, 1);
  const historyItems = toMessageItems(rows);

  // 合并去重：已存在的 id 保留本地版本（含发送后尚未持久化的临时消息 id 为负值），
  // 服务端真实 id 的原样补入。整体按 turn_num 升序保证顺序稳定。
  //
  // 竞态修复：发送后尚未持久化的临时消息 id 为负值（handleSend 用 --tempIdCounter），
  // 而当服务端稍后返回同一消息的真实正 id 行时，二者的 id 不同，按 id 去重会同时保留
  // 「临时负 id 副本」和「服务端正 id 行」，造成同一消息渲染两次。
  //
  // 因此对每个本地负 id 临时副本，直接在服务端历史行里按
  // 「同会话 + 同 turn_num + 同 role + 同 content」精确匹配其正 id 真身；
  // 命中则用服务端行替换（丢弃临时副本）。注意不能仅按 (session, turn, role) 归并查找
  // —— 同一轮次内可能出现多条同 role 的行（例如一次 AI 回合内的工具调用 + 最终回复，
  // add_messages 把整批写进同一个 turn_num），归并键会丢失其中若干行。逐行精确匹配
  // content 可保证不会跨行误替换。
  const mergedById = new Map<number, MessageItem>();
  const serverRowFor = (m: MessageItem) =>
    historyItems.find(
      (h) =>
        h.id >= 0 &&
        h.session_id === m.session_id &&
        h.turn_num === m.turn_num &&
        h.role === m.role &&
        h.content === m.content,
    );
  for (const m of chatMessages.value) {
    // 本地临时负 id 行：若服务端已返回同一逻辑消息的正 id 行，则跳过（用服务端行）。
    if (m.id < 0) {
      const serverRow = serverRowFor(m);
      // 命中：用服务端正 id 行替换临时副本，后续循环加入；此处占位以免重复
      if (serverRow) {
        mergedById.set(serverRow.id, serverRow);
        continue;
      }
    }
    mergedById.set(m.id, m);
  }
  for (const h of historyItems) {
    // 仅当本地没有同 id 的消息时才补入，避免覆盖流式过程中已更新的内容
    if (!mergedById.has(h.id)) mergedById.set(h.id, h);
  }
  // 按 turn_num 升序排序；同轮次内按 id 升序（与后端 messages 表
  // "ORDER BY turn_num ASC, id ASC" 一致）。此前用 id 降序会把同一轮次内
  // （用户消息 + AI 回复共享同一 turn_num）的插入顺序颠倒，导致刷新后
  // AI 回复跑到用户消息上面、最后一条 AI 回复不在最底部。
  chatMessages.value = [...mergedById.values()].sort(
    (a, b) => a.turn_num - b.turn_num || a.id - b.id,
  );

  // 若当前会话对象未初始化，补一个最小结构（消息已由 chatMessages 单独承载）
  if (!currentSession.value) {
    currentSession.value = { id: sessionId, title: '示例会话', createTime: '' } as SessionRecord;
  } else {
    currentSession.value.id = sessionId;
  }
};

/** 是否处于 AI 回复生成中 */
const isSending = ref(false);
/** 当前进行中的流式请求控制器（用于停止生成） */
let activeAgentController: AbortController | null = null;
/** 自增 id 计数器（用于本地临时消息，避免与真实 id 冲突） */
let tempIdCounter = 0;

/** 停止当前 AI 回复生成（前端本地中止 + 通知后端止停） */
const handleStop = () => {
  activeAgentController?.abort();
  activeAgentController = null;
  isSending.value = false;
};

/**
 * 处理输入框发送：把用户消息加入列表，并通过流式请求（Tauri IPC 或浏览器 WebSocket）获取 AI 回复。
 *
 * @param text 用户输入内容
 */
const handleSend = async (text: string) => {
  const sessionId = currentSessionId.value || 'main';

  // 确保当前会话已初始化
  if (!currentSession.value) {
    currentSession.value = { id: sessionId, title: '示例会话', createTime: '' } as SessionRecord;
    currentSessionId.value = sessionId;
  }

  // 计算下一轮次号：取当前消息中最大 turn_num + 1，而非按数组长度。
  // 后端 add_messages 以「一次对话」为一轮，用户消息 + AI 回复（含工具调用行）
  // 共享同一 turn_num（每轮递增 1）。若按 chatMessages.length 推算，
  // 一轮含多条消息时（1 用户 + 1 AI + 可能的工具行）会与实际持久化的
  // turn_num 错位，刷新后历史顺序与该轮次号不一致。
  const turnNum =
    chatMessages.value.reduce((max, m) => Math.max(max, m.turn_num), 0) + 1;

  // 携带本次待发送的图片（发送时取走并清空待发送列表）
  const imageBase64List = selectedImages.value.map((img) => img.base64);

  // 追加用户消息（本地即时显示）
  const userMsg: MessageItem = {
    session_id: sessionId,
    role: CHAT_ROLE.USER,
    content: text,
    images: imageBase64List,
    id: --tempIdCounter,
    turn_num: turnNum,
    timestamp: new Date().toISOString()
  };

  // 追加 AI 占位消息（内容随流式块逐步填充）。
  // 与用户消息共享同一 turn_num（后端会把整批写进同一个 turn_num）。
  const aiMsg: MessageItem = {
    session_id: sessionId,
    role: CHAT_ROLE.AI,
    content: '',
    id: --tempIdCounter,
    turn_num: turnNum,
    timestamp: new Date().toISOString()
  };

  chatMessages.value = [...chatMessages.value, userMsg, aiMsg];

  // 发送后清空待发送图片与输入区
  selectedImages.value = [];

  isSending.value = true;
  try {
    const req: ChatRequest = { text };
    if (imageBase64List.length > 0) req.image_base64_list = imageBase64List;
    activeAgentController = postAgentStream(
      sessionId,
      req,
      (chunk: string) => {
        aiMsg.content += chunk;
        // 直接替换整个数组以触发响应式更新（aiMsg 本身是引用，content 已在原地累加）
        chatMessages.value = [...chatMessages.value];
      },
      () => {
        // 流正常结束，解锁输入框
        activeAgentController = null;
        isSending.value = false;
      },
      (err) => {
        activeAgentController = null;
        aiMsg.content = `（回复失败：${String(err)}）`;
        isSending.value = false;
      }
    );
  } catch (e) {
    // 同步抛错（罕见），此时流未启动，直接解锁
    activeAgentController = null;
    aiMsg.content = `（发送失败：${String(e)}）`;
    isSending.value = false;
  }
};

/**
 * 已选图片（base64 形式，随消息发送）。
 * 仅在「本地预览 + 发送」期间存在，发送/取消后清空。
 */
const selectedImages = ref<{ base64: string; name: string }[]>([]);

/** 隐藏的图片文件选择框 */
const imageFileInput = useTemplateRef<HTMLInputElement>('imageFileInputRef');

/** 读取图片文件为 DataURL（含 data:image/...;base64 前缀，需剥离前缀再发送） */
const readImageFile = (file: File): Promise<string> =>
  new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });

/** 移除一张已选图片 */
const removeImage = (index: number) => {
  selectedImages.value.splice(index, 1);
  selectedImages.value = [...selectedImages.value];
};

/** 触发系统图片文件选择 */
const triggerImagePicker = () => {
  imageFileInput.value?.click();
};

/** 图片选择回调：读取为 base64 并加入待发送列表 */
const onImageSelected = async (event: Event) => {
  const input = event.target as HTMLInputElement;
  // 先拷贝为普通数组副本再重置 input.value。
  // input.files 是「活」的 FileList —— 一旦把 value 置空，浏览器会立即清空该 FileList，
  // 若此后才读取 files 会得到空数组，导致 selectedImages 永不入库、预览区不显示。
  const files = Array.from(input.files ?? []);
  input.value = ''; // 允许重复选择同一文件
  if (files.length === 0) return;

  for (const file of files) {
    if (!file.type.startsWith('image/')) continue;
    try {
      const dataUrl = await readImageFile(file);
      // data:image/png;base64,xxxxx -> 仅保留 base64 部分
      const base64 = dataUrl.split(',')[1] ?? '';
      selectedImages.value.push({ base64, name: file.name });
    } catch (e) {
      console.warn('[onImageSelected] 读取图片失败：', file.name, e);
    }
  }
  selectedImages.value = [...selectedImages.value];
};

/** 工具触发 */
const handleOperate = (type: string, event: string) => {
  if (!event || !type) return;
  // 头部区域
  if (type === 'headerBar') {
    switch (event) {
      case 'userCenter':
        return;
      case 'knowledgeBase':
        return;
      default:
        return;
    }
  } else {
    // 工具栏
    switch (event) {
      case 'createSession':
        handleCreateSession();
        return;
      case 'knowledgeBase':
        return;
      case 'uploadFile':
        return;
      case 'uploadImage':
        triggerImagePicker();
        return;
      default:
        return;
    }
  }
};

/** 新增会话 */
const handleCreateSession = () => {};

/**
 * 会话切换：先清空当前渲染列表与会话对象，再加载新会话历史。
 *
 * 必须清空，否则 loadSessionHistory 是「合并」语义，会把上一个会话的消息
 * 混进新会话。清空后加载期间 ChatBox 短暂为空是可接受的（切换反馈）。
 */
const handleToggleSession = (id: string) => {
  if (currentSessionId.value === id) return;
  currentSession.value = undefined;
  chatMessages.value = [];
  currentSessionId.value = id;
  loadSessionHistory(id);
  isSidebarOpen.value = false;
};

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

const headerToolsMenuRef = ref<InstanceType<typeof Menu>>();
const openHeaderMenu = (event: Event) => {
  headerToolsMenuRef.value?.toggle(event);
};

// 首屏加载默认会话(main)的历史消息,接收合并后的列表渲染到 ChatBox
loadSessionHistory('main');
// 挂载后从服务端加载角色配置（头像 + 名字）
onMounted(() => {
  loadCharacter();
});
</script>
