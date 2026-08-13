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
    <!-- 左侧-历史记录区域 -->
    <!-- 移动端：固定定位，默认隐藏，通过按钮切换 -->
    <!-- md：固定定位，显示宽度 280px -->
    <!-- lg：相对定位，显示宽度 360px -->
    <div
      :class="[
        'flex flex-col px-4 relative h-full w-[280px] md:w-[280px] lg:w-[360px]',
        'border-r border-solid border-gray-light bg-[#fff] dark:border-gray-dark dark:bg-[#2a2a36]'
      ]">
      <!-- LOGO区域 -->
      <div class="flex items-center h-15 text-xl">🍊{{ characterInfo.aiName }}</div>
      <!-- 新建对话 -->
      <Button
        icon="pi pi-comment"
        :label="t('toolbar.newChat')"
        class="mt-3 mb-3"
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
          v-model:selectedList="selectedSessionIds" />
      </div>
      <div class="h-17 flex items-center justify-between">
        <div class="flex items-center justify-center gap-1">
          <Checkbox
            v-model="isCheckAllSession"
            :indeterminate="isIndeterminate"
            binary />
          <span>{{ t('history.selectAll') }}</span>
        </div>
        <Button
          icon="pi pi-trash"
          :label="t('history.batchDelete')" />
      </div>
    </div>

    <!-- 右侧-会话主体区域 -->
    <div class="flex flex-col flex-1 h-full bg-white dark:bg-[#131619]">
      <!-- 顶部工具栏 -->
      <div
        class="flex md:justify-end justify-between box-border border-b border-solid border-gray-light dark:border-gray-dark p-3 h-15">
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
            :model="headerMenuModel"
            :popup="true"></Menu>
          <div class="hidden md:flex justify-end items-center flex-1 gap-3">
            <Button
              :icon="tool.icon"
              v-for="tool in headerTools"
              :key="tool.event"
              :title="t(tool.title)"
              @click="handleOperate('headerBar', tool.event)"
              :label="t(tool.toolName)"
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
              :title="t('chatBox.removeImage')"
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
              :label="t(tool.toolName)"
              @click="handleOperate('toolBar', tool.event)"
              size="small"
              variant="text" />
          </template>
          <template class="block sm:hidden">
            <Button
              v-for="tool in tools"
              :key="tool.event"
              :icon="tool.icon"
              :aria-label="t(tool.toolName)"
              @click="handleOperate('toolBar', tool.event)"
              size="small"
              variant="text" />
          </template>
        </div>
        <!-- 输入框 -->
        <ChatInputBox :sending="isSending" @send="handleSend" @stop="handleStop" />
      </div>
    </div>

    <!-- 技能查看弹窗 -->
    <SkillsDialog v-model="showSkillsDialog" />

    <!-- 系统配置弹窗 -->
    <ConfigDialog v-model="showConfigDialog" @saved="loadCharacter" />

    <!-- HITL 审批弹窗 -->
    <Dialog
      v-model:visible="hitlRequest"
      :header="t('hitl.title', 'Action Requires Approval')"
      :modal="true"
      :closable="false"
      class="w-[90vw] md:w-[500px]">
      <div class="flex flex-col gap-3">
        <div class="text-sm text-gray-500">{{ t('hitl.tool', 'Tool') }}: <span class="font-bold">{{ hitlRequest?.tool_name }}</span></div>
        <div v-if="hitlRequest?.description" class="text-sm whitespace-pre-wrap">{{ hitlRequest.description }}</div>
        <div v-if="hitlRequest?.tool_args && Object.keys(hitlRequest.tool_args).length > 0" class="text-xs bg-gray-50 dark:bg-gray-800 p-3 rounded-lg overflow-auto max-h-40">
          <pre class="m-0">{{ JSON.stringify(hitlRequest.tool_args, null, 2) }}</pre>
        </div>
      </div>
      <template #footer>
        <div class="flex gap-2 justify-end">
          <Button
            :label="t('hitl.reject', 'Reject')"
            icon="pi pi-times"
            severity="danger"
            @click="handleHitlDecision('reject')" />
          <Button
            :label="t('hitl.approve', 'Approve')"
            icon="pi pi-check"
            @click="handleHitlDecision('approve')" />
        </div>
      </template>
    </Dialog>
  </div>
</template>

<script lang="ts" setup>
// components
import HistoryItem from './components/HistoryItem.vue';
import ModeSwitch from './components/ModeSwitch.vue';
import ChatBox from './components/ChatBox.vue';
import SkillsDialog from './components/SkillsDialog.vue';
import ConfigDialog from './components/ConfigDialog.vue';
// function
import { computed, onMounted } from 'vue';
import { useI18n } from 'vue-i18n';
import type { SessionRecord, MessageItem, HitlRequestData } from './type.ts';
import { CHAT_ROLE } from './type.ts';
import type { CachedMessage } from '@/composables/db';
import { tools, headerTools } from './config';
import { Menu } from 'primevue';
import { readCharacter } from '@/composables/bridge';
import type { ChatRequest, AgentChunkType, HitlResponse } from '@/composables/bridge';

// 图片预览
const { openPreview } = useImagePreview();

const { t } = useI18n();

/** 技能查看弹窗开关 */
const showSkillsDialog = ref(false);

/** 系统配置弹窗开关 */
const showConfigDialog = ref(false);

/**
 * 角色显示信息（来自服务端 character.json）
 * - user.avatar / assistant.avatar 为相对 `static/` 的路径，拼接 base URL 得到完整图片地址
 */
const backendBaseUrl = ref<string>(import.meta.env.VITE_API_BACK_URL || 'http://localhost:8080');
const characterInfo = ref<{ userName: string; userAvatar: string; aiName: string; aiAvatar: string }>({
  userName: t('chatBox.defaultUserName'),
  userAvatar: '',
  aiName: t('chatBox.defaultAiName'),
  aiAvatar: ''
});

/** 解析角色配置：把 `avatar/xxx.jpg` 形式的路径拼接成完整静态资源 URL */
const resolveCharacter = (data?: Record<string, Record<string, string>>) => {
  const base = (backendBaseUrl.value || '').replace(/\/+$/, '');
  const user = data?.user ?? {};
  const assistant = data?.assistant ?? {};
  const resolveUrl = (path?: string) => (path ? `${base}/static/${path.replace(/^\/+/, '')}` : '');
  characterInfo.value = {
    userName: user.name || t('chatBox.defaultUserName'),
    userAvatar: resolveUrl(user.avatar),
    aiName: assistant.name || t('chatBox.defaultAiName'),
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
const historyList = ref<SessionRecord[]>([]);

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
    timestamp: row.timestamp ?? '',
    // 透传工具字段（历史消息中 role=tool 的行会有值）
    toolName: row.tool_name ?? undefined,
    toolStatus: (row.tool_status as 'running' | 'done') ?? undefined,
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
    currentSession.value = { id: sessionId, title: t('history.newSession'), createTime: '' } as SessionRecord;
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

/** HITL 审批请求（当 agent 暂停等待人工审批时设置） */
const hitlRequest = ref<HitlRequestData | null>(null);

/** 处理 HITL 审批请求：显示审批弹窗 */
const handleHitlRequest = (data: HitlRequestData) => {
  hitlRequest.value = data;
};

/** 用户审批/拒绝 HITL 请求 */
const handleHitlDecision = (decision: 'approve' | 'reject', message: string = '') => {
  if (!activeAgentController) return;
  const sender = (activeAgentController as any).sendHitlResponse as
    | ((response: HitlResponse) => void) | null;
  if (sender) {
    sender({ decision, message });
  }
  hitlRequest.value = null;
};

/** 停止当前 AI 回复生成（前端本地中止 + 通知后端止停） */
const handleStop = () => {
  activeAgentController?.abort();
  activeAgentController = null;
  isSending.value = false;
};

/**
 * 处理输入框发送：把用户消息加入列表，并通过流式请求（Tauri IPC 或浏览器 WebSocket）获取 AI 回复。
 *
 * 流式回复动态分段：后端按 chunk type（text / tool_start / tool_end）区分对话文本
 * 与工具调用，前端据此实时创建/更新独立的消息气泡——对话一个框、工具调用一个框。
 *
 * @param text 用户输入内容
 */
const handleSend = async (text: string) => {
  const sessionId = currentSessionId.value || 'main';

  // 确保当前会话已初始化
  if (!currentSession.value) {
    currentSession.value = { id: sessionId, title: t('history.newSession'), createTime: '' } as SessionRecord;
    currentSessionId.value = sessionId;
  }

  // 计算下一轮次号：取当前消息中最大 turn_num + 1，而非按数组长度。
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

  // 初始 AI 占位消息（内容随流式块逐步填充）
  const aiMsg: MessageItem = {
    session_id: sessionId,
    role: CHAT_ROLE.AI,
    content: '',
    id: --tempIdCounter,
    turn_num: turnNum,
    timestamp: new Date().toISOString()
  };

  // 本轮所有 AI/TOOL 消息（用于流式回调中动态追加/更新）
  const turnMsgs: MessageItem[] = [aiMsg];

  chatMessages.value = [...chatMessages.value, userMsg, aiMsg];

  // 发送后清空待发送图片与输入区
  selectedImages.value = [];

  isSending.value = true;

  /**
   * 流式 chunk 回调：按 type 动态管理消息分段。
   *
   * - text: 追加到最后一条 AI 消息；若最后一条是 TOOL 消息则新建 AI 消息
   * - tool_start: 新建一条 TOOL 消息（status=running）
   * - tool_end: 将最后一条 TOOL 消息标记为 done
   */
  const onStreamChunk = (content: string, type: AgentChunkType) => {
    if (type === 'text') {
      const last = turnMsgs[turnMsgs.length - 1];
      if (last && last.role === CHAT_ROLE.AI) {
        last.content += content;
      } else {
        const newAi: MessageItem = {
          session_id: sessionId,
          role: CHAT_ROLE.AI,
          content,
          id: --tempIdCounter,
          turn_num: turnNum,
          timestamp: new Date().toISOString()
        };
        turnMsgs.push(newAi);
        chatMessages.value = [...chatMessages.value, newAi];
      }
    } else if (type === 'tool_start') {
      const toolMsg: MessageItem = {
        session_id: sessionId,
        role: CHAT_ROLE.TOOL,
        content: '',
        toolName: content,
        toolStatus: 'running',
        id: --tempIdCounter,
        turn_num: turnNum,
        timestamp: new Date().toISOString()
      };
      turnMsgs.push(toolMsg);
      chatMessages.value = [...chatMessages.value, toolMsg];
    } else if (type === 'tool_end') {
      // 标记最近的 TOOL 消息为已完成
      for (let i = turnMsgs.length - 1; i >= 0; i--) {
        if (turnMsgs[i].role === CHAT_ROLE.TOOL) {
          turnMsgs[i].toolStatus = 'done';
          break;
        }
      }
    }
    // 触发响应式更新
    chatMessages.value = [...chatMessages.value];
  };

  try {
    const req: ChatRequest = { text };
    if (imageBase64List.length > 0) req.image_base64_list = imageBase64List;
    activeAgentController = postAgentStream(
      sessionId,
      req,
      onStreamChunk,
      () => {
        // 流正常结束，解锁输入框
        activeAgentController = null;
        isSending.value = false;
      },
      (err) => {
        activeAgentController = null;
        aiMsg.content = t('errors.replyFailed', { reason: String(err) });
        isSending.value = false;
      },
      handleHitlRequest,
    );
  } catch (e) {
    // 同步抛错（罕见），此时流未启动，直接解锁
    activeAgentController = null;
    aiMsg.content = t('errors.sendFailed', { reason: String(e) });
    isSending.value = false;
  }
};

/**
 * 已选图片（base64 形式，随消息发送）。
 * 仅在「本地预览 + 发送」期间存在，发送/取消后清空。
 */
const selectedImages = ref<{ base64: string; name: string }[]>([]);

/** 单条消息允许附带的最大图片数量 */
const MAX_SELECTED_IMAGES = 10;

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

/** 图片选择回调：读取为 base64 并加入待发送列表（上限 MAX_SELECTED_IMAGES） */
const onImageSelected = async (event: Event) => {
  const input = event.target as HTMLInputElement;
  // 先拷贝为普通数组副本再重置 input.value。
  // input.files 是「活」的 FileList —— 一旦把 value 置空，浏览器会立即清空该 FileList，
  // 若此后才读取 files 会得到空数组，导致 selectedImages 永不入库、预览区不显示。
  const files = Array.from(input.files ?? []);
  input.value = ''; // 允许重复选择同一文件
  if (files.length === 0) return;

  // 数量上限：超出的部分直接截断并提示
  const remaining = MAX_SELECTED_IMAGES - selectedImages.value.length;
  if (remaining <= 0) {
    alert(t('chatInput.maxImages', { count: MAX_SELECTED_IMAGES }));
    return;
  }
  const accepted = files.slice(0, remaining);
  if (files.length > remaining) {
    alert(t('chatInput.maxImagesExceed', { count: MAX_SELECTED_IMAGES, extra: files.length - remaining }));
  }

  for (const file of accepted) {
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
      case 'knowledgeBase':
        return;
      case 'skills':
        showSkillsDialog.value = true;
        return;
      case 'systemConfig':
        showConfigDialog.value = true;
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

/** 新增会话：生成随机 session_id，创建新会话窗口并切换 */
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
  currentSession.value = newSession;
  currentSessionId.value = sessionId;
  chatMessages.value = [];
};

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

/** 移动端头部工具菜单（为每个工具绑定 command 回调，保证点击可用） */
const headerMenuModel = computed(() =>
  headerTools.map((tool) => ({
    label: t(tool.title),
    icon: tool.icon,
    command: () => handleOperate('headerBar', tool.event),
  })),
);

// 首屏加载默认会话(main)的历史消息，获取合并后的列表渲染到 ChatBox
loadSessionHistory('main');
// 挂载后从服务端加载角色配置（头像 + 名字）
onMounted(() => {
  loadCharacter();
});
</script>
