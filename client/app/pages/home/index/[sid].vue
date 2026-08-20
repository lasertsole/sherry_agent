<template>
  <div class="flex flex-col flex-1 h-full bg-transparent dark:bg-transparent">
    <!-- 聊天主体 / 空态（有会话 sid 时始终显示聊天面板；仅无 sid 的根路径显示"开启新对话"） -->
    <template v-if="sessionId">
      <!-- HITL 审批卡片：非模态，置于历史消息区顶部（不遮最新会话），
           占用独立位置不影响其余消息查看；可随内容自然撑开。 -->
      <div
        v-if="hitlRequest"
        class="shrink-0 mx-2 mt-2 bg-white dark:bg-[#131619] rounded-lg border border-solid border-gray-light dark:border-gray-dark shadow-lg">
        <div class="flex flex-col gap-3 p-3">
          <div class="text-sm font-semibold">{{ t('hitl.title', 'Action Requires Approval') }}</div>
          <div class="text-sm text-gray-500">
            {{ t('hitl.tool', 'Tool') }}: <span class="font-bold">{{ hitlRequest?.tool_name }}</span>
          </div>
          <div
            v-if="hitlRequest?.description"
            class="text-sm whitespace-pre-wrap">
            {{ hitlRequest.description }}
          </div>
          <div
            v-if="hitlRequest?.tool_args && Object.keys(hitlRequest.tool_args).length > 0"
            class="text-xs bg-gray-50 dark:bg-gray-800 p-3 rounded-lg overflow-auto max-h-40">
            <pre class="m-0">{{ JSON.stringify(hitlRequest.tool_args, null, 2) }}</pre>
          </div>
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
        </div>
      </div>
      <ChatBox
        :messages="chatMessages"
        :user-avatar="characterInfo.userAvatar"
        :ai-avatar="characterInfo.aiAvatar"
        :user-name="characterInfo.userName"
        :ai-name="characterInfo.aiName" />
      <!-- 图片预览区（独立于输入框上方，避免挤压 h-40 输入框导致发送按钮上移 / ✕ 按钮被裁剪） -->
      <template v-if="selectedImages.length > 0">
        <div
          class="flex items-center gap-2 px-2 py-2 border-t border-solid border-gray-light dark:border-gray-dark overflow-x-auto">
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
      <!-- 音频预览区（独立于输入框上方，与图片预览区同级） -->
      <template v-if="selectedAudios.length > 0">
        <div
          class="flex items-center gap-2 px-2 py-2 border-t border-solid border-gray-light dark:border-gray-dark overflow-x-auto">
          <div
            v-for="(audio, idx) in selectedAudios"
            :key="idx"
            class="relative shrink-0 group">
            <div
              class="flex items-center gap-2 px-3 py-2 rounded-lg border border-solid border-gray-light dark:border-gray-dark bg-white dark:bg-gray-800">
              <span class="pi pi-volume-down text-xs text-[#6B7280]"></span>
              <span class="text-xs font-medium text-[#111827] dark:text-[#E5E7EB] max-w-32 truncate">{{
                audio.name
              }}</span>
            </div>
            <button
              type="button"
              :title="t('chatBox.removeAudio')"
              class="absolute top-0.5 right-0.5 z-10 w-6 h-6 flex items-center justify-center rounded-full bg-[#ef4444] text-white text-sm leading-none shadow-md cursor-pointer"
              @click="removeAudio(idx)">
              ✕
            </button>
          </div>
        </div>
      </template>
      <!-- 视频预览区（独立于输入框上方，与图片/音频预览区同级） -->
      <template v-if="selectedVideos.length > 0">
        <div
          class="flex items-center gap-2 px-2 py-2 border-t border-solid border-gray-light dark:border-gray-dark overflow-x-auto">
          <div
            v-for="(video, idx) in selectedVideos"
            :key="idx"
            class="relative shrink-0 group">
            <video
              :src="`data:video/*;base64,${video.base64}`"
              class="w-32 h-20 object-cover rounded-lg border border-solid border-gray-light dark:border-gray-dark"
              muted
              playsinline
              preload="metadata" />
            <button
              type="button"
              :title="t('chatBox.removeVideo')"
              class="absolute top-0.5 right-0.5 z-10 w-6 h-6 flex items-center justify-center rounded-full bg-[#ef4444] text-white text-sm leading-none shadow-md cursor-pointer"
              @click="removeVideo(idx)">
              ✕
            </button>
          </div>
        </div>
      </template>
      <!-- 聊天输入框区域（relative 定位父级，供其他悬浮元素使用） -->
      <div class="relative">
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
            <!-- 隐藏的图片文件选择框：由工具栏图片按钮通过 triggerImagePicker() 触发 -->
            <input
              ref="imageFileInputRef"
              type="file"
              accept="image/*"
              multiple
              class="hidden"
              @change="onImageSelected" />
            <!-- 隐藏的音频文件选择框：由工具栏音频按钮通过 triggerAudioPicker() 触发 -->
            <input
              ref="audioFileInputRef"
              type="file"
              accept="audio/*"
              multiple
              class="hidden"
              @change="onAudioSelected" />
            <!-- 隐藏的视频文件选择框：由工具栏视频按钮通过 triggerVideoPicker() 触发 -->
            <input
              ref="videoFileInputRef"
              type="file"
              accept="video/*"
              multiple
              class="hidden"
              @change="onVideoSelected" />
          </div>
          <!-- 输入框：存在待审批的 HITL 请求时禁止输入/发送，并提示等待审批 -->
          <ChatInputBox
            ref="chatInputBoxRef"
            v-model:draft="draft"
            :sending="isSending"
            :disabled="!!hitlRequest"
            :disabled-text="t('chatInput.waitingApproval')"
            @send="handleSend"
            @stop="handleStop" />
        </div>
      </div>
    </template>
    <!-- 空态：无消息时显示居中的"开启新对话"按钮 -->
    <div
      v-else
      class="flex-1 flex flex-col items-center justify-center gap-4">
      <div class="flex flex-col items-center gap-2">
        <span class="pi pi-comments text-4xl text-[#9CA3AF]"></span>
        <p class="text-base font-medium text-[#6B7280] dark:text-[#9CA3AF]">{{ t('history.noSessions') }}</p>
      </div>
      <Button
        icon="pi pi-plus"
        :label="t('toolbar.newChat')"
        @click="handleCreateSession" />
    </div>
  </div>
</template>

<script lang="ts" setup>
// components
import ChatBox from '../components/ChatBox.vue';
// function
import { computed, onActivated, onDeactivated, onMounted, onUnmounted, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import { useRoute, useRouter } from 'vue-router';
import type { MessageItem, HitlRequestData } from '../type.ts';
import type { MultiModalMessage } from '@/types/message';
import { CHAT_ROLE } from '../type.ts';
import type { CachedCharacter, CachedMessage } from '@/composables/db';
import {
  DEFAULT_CACHED_CHARACTER,
  cacheCharacter,
  readCachedCharacter,
  cacheSessionMeta,
  saveDraftTurn,
  readDraftTurns,
  clearDraftTurn,
  clearDraftSession,
  type DraftTurn
} from '@/composables/db';
import { tools } from '../config';
import { resumeHitl, type AgentChunkType } from '@/composables/bridge';
import {
  get_history_by_turn_page,
  getPendingInterrupt,
  postAgentStream,
  SESSION_ABORT_STREAM_EVENT
} from '@/composables/messages';
import { on, off } from '@/composables/mitt';

// 图片预览
const { openPreview } = useImagePreview();

const { t } = useI18n({ useScope: 'local' });
const route = useRoute();
const router = useRouter();

/** 当前会话 ID（来自路由参数 [sid]） */
const sessionId = computed(() => String(route.params.sid ?? ''));

/**
 * 本实例「冻结」的会话 ID：每个 [sid].vue 实例由 KeepAlive 按 page-key（=sid）独立缓存，
 * 一个实例固定只属于一个 sid，不会在切换会话时被复用。
 *
 * 为什么不能只靠 `sessionId`：`useRoute()` 返回的是全局共享的 reactive route 单例，
 * 被**所有**缓存实例引用。当浏览器从 sidA 切到 sidB 时，`route.params.sid` 全局变成 'sidB'，
 * 于是 sidA 实例（即便已被 KeepAlive 缓存、处于非激活态）的 `sessionId` computed 也会跟着
 * 重新计算成 'sidB'。任何依赖 `sessionId` 的事件处理器（如按 sid 比对是否命中删除广播）
 * 都会因此误判。故在本实例创建时把 sid 冻结成常量 `mySid`，凡「本实例到底属于谁」的判断
 * 一律用它，杜绝跨实例串扰。
 */
const mySid = String(route.params.sid ?? '');

/**
 * 本实例是否处于「激活」态（KeepAlive 缓存中处于隐藏状态时为 false）。
 * KeepAlive 缓存**不会暂停**被缓存实例的响应式 watch/effect —— 切换到 sidB 时，
 * 所有非激活实例的全局 `route` 变化仍会触发它们的 `watch(sessionId)`。
 * 用该标志区分「本实例此刻是否正被显示」，配合 `mySidLoaded` 实现：
 *  - 切走（非激活）→ 保留内存状态，绝不执行破坏性清空；
 *  - 切回（重新激活）→ 已加载过则原样恢复（草稿/滚动/流式/HITL），不再重载。
 */
const isActive = ref(false);
onActivated(() => {
  isActive.value = true;
  // 返回本会话时刷新可能仍待审批的 HITL 卡（幂等：已有卡/进行中则早退）
  if (mySid) restorePendingHitl(mySid);
});
onDeactivated(() => {
  isActive.value = false;
});

/**
 * 本实例是否已为「自己的会话（mySid）」加载过历史。
 * 首个 KeepAlive 缓存实例只在**首次**挂载时加载一次历史；之后再切回本会话题时
 * （sessionId 又从别的 sid 变回 mySid）只需原样恢复内存态，不重复清空加载，
 * 从而保住未持久化的草稿、滚动位置与仍在后台流式的消息。
 */
let mySidLoaded = false;

/**
 * 角色显示信息（来源为本地 Dexie 按会话缓存的快照，见 `db.ts` 的 `CachedCharacter`）。
 * - `userAvatar` / `aiAvatar` 为 base64 data URL（用户自定义）或 `/avatar/xxx.jpg` 相对 URL（内置默认），`<img>` 均可直接渲染。
 * - 每次切换/新建会话时从对应会话快照（或全局待定 profile）刷新，旧会话保留各自快照。
 */
const characterInfo = ref<{ userName: string; userAvatar: string; aiName: string; aiAvatar: string }>({
  userName: DEFAULT_CACHED_CHARACTER.userName,
  userAvatar: DEFAULT_CACHED_CHARACTER.userAvatar,
  aiName: DEFAULT_CACHED_CHARACTER.aiName,
  aiAvatar: DEFAULT_CACHED_CHARACTER.aiAvatar
});

/** 默认角色显示信息（内置：远野汉娜 / 橘雪莉 + 默认头像 URL，见 `defaultCharacter.ts`） */
const defaultCharacter = (): { userName: string; userAvatar: string; aiName: string; aiAvatar: string } => ({
  userName: DEFAULT_CACHED_CHARACTER.userName,
  userAvatar: DEFAULT_CACHED_CHARACTER.userAvatar,
  aiName: DEFAULT_CACHED_CHARACTER.aiName,
  aiAvatar: DEFAULT_CACHED_CHARACTER.aiAvatar
});

// ── 聊天区背景图（由 home/index.vue 根容器统一渲染） ────────
// 背景图是全局配置，绑定在 home/index.vue 的根容器（铺满整个窗口，含左侧会话列表），
// 由共享单例 useChatBackground 在保存后即时更新，本页无需再加载/渲染背景图。
// 本页根 div 已在 template 中设为 bg-transparent（浅色主题），让根容器的背景图透出。

/**
 * 将一份角色快照映射为 `characterInfo`（空消息段回退到内置默认值）。
 */
const applyCharacterSnapshot = (snap?: Pick<CachedCharacter, 'userName' | 'userAvatar' | 'aiName' | 'aiAvatar'>) => {
  const defaultInfo = defaultCharacter();
  characterInfo.value = snap
    ? {
        userName: snap.userName?.trim() ? snap.userName : defaultInfo.userName,
        userAvatar: snap.userAvatar ?? defaultInfo.userAvatar,
        aiName: snap.aiName?.trim() ? snap.aiName : defaultInfo.aiName,
        aiAvatar: snap.aiAvatar ?? defaultInfo.aiAvatar
      }
    : defaultInfo;
};

/**
 * 确保指定会话已锁定自己的角色快照，并把 `characterInfo` 更新为该会话的显示信息。
 *
 * 命名逻辑：系统配置-角色配置编辑的是「全局待定 profile」（`GLOBAL_SESSION_KEY` 行）。
 * 每个会话在首次打开时，把当时的全局 profile 拷贝并锁定到自己的 `session_id` 行；
 * 之后全局更新（改头像/名字）不再作用于已锁定快照的旧会话，仅新会话会取到最新全局值。
 *
 * @param sessionId 会话 ID
 */
const ensureSessionCharacter = async (sessionId: string) => {
  try {
    const [globalSnap, sessionSnap] = await Promise.all([
      readCachedCharacter('__global__'),
      readCachedCharacter(sessionId)
    ]);
    // 会话已有快照（旧会话锁定的头像/名字）→ 直接用快照，不受全局变更影响。
    if (sessionSnap) {
      applyCharacterSnapshot(sessionSnap);
      return;
    }
    // 会话尚无快照（新建或从未打开过的会话）→ 用全局 profile 快照并锁定。
    // 注意：`base` 可能是全局行（含 session_id=GLOBAL_SESSION_KEY），
    // 必须用 `...base` 之后显式覆盖 session_id，避免把真实会话的 key 写进全局行。
    const base = globalSnap ?? defaultCharacter();
    const locked: CachedCharacter = { ...base, session_id: sessionId };
    await cacheCharacter(locked);
    applyCharacterSnapshot(locked);
  } catch (error) {
    // Dexie 读写异常时保留当前显示，不阻塞聊天。
    console.warn('[ensureSessionCharacter] 读取角色快照失败：', error);
  }
};

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
          : ''
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
const toMessageItems = (rows: CachedMessage[]): MessageItem[] => {
  /**
   * 工具调用的实际参数（args）并不存在 role=tool 的行上，
   * 而是落在与之配对的前驱 role=ai 行（其 tool_calls 列已持久化为 JSON）。
   *
   * 因此这里先对全部 ai 行做一次扫描，把每个 tool call id 对应的
   * { name, args } 索引起来，再在 tool 行上按 tool_call_id 精确配对取回。
   *
   * tool_calls 的原始形态：
   *  - 来自后端历史接口时已是解析后的对象数组 [ { id, name, args, type } ]；
   *  - 来自本地 Dexie 缓存时可能仍是 JSON 字符串，故做一次安全解析。
   */
  const toolCallById = new Map<string, { name?: string; args?: Record<string, unknown> }>();
  for (const row of rows) {
    if (row.role !== CHAT_ROLE.AI) continue;
    let calls: unknown = row.tool_calls;
    if (typeof calls === 'string') {
      try {
        calls = JSON.parse(calls);
      } catch {
        calls = null;
      }
    }
    if (!Array.isArray(calls)) continue;
    for (const call of calls) {
      if (typeof call !== 'object' || call === null) continue;
      const c = call as { id?: unknown; name?: unknown; args?: unknown };
      if (typeof c.id !== 'string' || !c.id) continue;
      toolCallById.set(c.id, {
        name: typeof c.name === 'string' ? c.name : undefined,
        args: typeof c.args === 'object' && c.args !== null ? (c.args as Record<string, unknown>) : undefined
      });
    }
  }

  return rows.map(row => {
    // role=tool 的行：按 tool_call_id 在 ai 行索引中提取 args，补齐名称与结果
    if (row.role === CHAT_ROLE.TOOL && typeof row.tool_call_id === 'string') {
      const callInfo = toolCallById.get(row.tool_call_id);
      const rawStatus = row.tool_status ?? 'success';
      // 后端 tool_status 存的是 success/failed/error，前端展示层统一为 done/failed/error
      const toolStatus: MessageItem['toolStatus'] =
        rawStatus === 'success' ? 'done' : (rawStatus as MessageItem['toolStatus']);
      return {
        session_id: row.session_id,
        role: CHAT_ROLE.TOOL,
        content: normalizeContent(row.content),
        images: row.images ?? undefined,
        id: row.id,
        turn_num: row.turn_num,
        timestamp: row.timestamp ?? '',
        // 名称优先取配对 ai 行的 tool call 名（tool_name 列也可能缺失）
        toolName: callInfo?.name ?? row.tool_name ?? undefined,
        toolStatus,
        // 从配对 ai 行取回真实执行参数
        toolArgs: callInfo?.args,
        toolResult: normalizeContent(row.content)
      };
    }

    return {
      session_id: row.session_id,
      role: row.role as CHAT_ROLE,
      content: normalizeContent(row.content),
      // 透传图片数组：用户消息为 base64，AI 消息为持久化文件路径，交由 ChatBox 区分渲染
      images: row.images ?? undefined,
      // 透传音频/视频数组（与图片同级：用户消息为 base64，AI 消息为持久化文件路径）
      audios: row.audios ?? undefined,
      videos: row.videos ?? undefined,
      id: row.id,
      turn_num: row.turn_num,
      timestamp: row.timestamp ?? '',
      // 透传工具字段（历史消息中 role=tool 的行会有值）
      toolName: row.tool_name ?? undefined,
      toolStatus: (row.tool_status as 'running' | 'done') ?? undefined,
      // 透传模型思考/推理过程（后端 messages 表的 reasoning 字段，仅在 AI 行有值）
      reasoning: row.reasoning ?? null,
      // 透传模型元数据（后端 messages 表的 model_name/input_tokens/output_tokens，仅在 AI 行有值）
      modelName: row.model_name ?? undefined,
      inputTokens: row.input_tokens ?? undefined,
      outputTokens: row.output_tokens ?? undefined
    };
  });
};

/**
 * 加载指定会话的历史消息（本地缓存优先，后台合并服务端增量），
 * 合并进 `chatMessages`（按 id 去重），供 ChatBox 渲染。
 *
 * 修复：不再整体重建 `currentSession.value`（那会覆盖用户已发送的本地消息，
 * 导致「发送后列表被清空」）。只把历史行合并进单一列表，已存在的消息保留。
 */
const loadSessionHistory = async (sessionId: string) => {
  const rows = await get_history_by_turn_page(sessionId, 0, 10, 1);
  const historyItems = toMessageItems(rows);

  // 合并去重：已存在的 id 保留本地版本（含发送后尚未持久化的临时消息 id 为负值），
  // 服务端真实 id 的原样补入。整体按 turn_num 升序保证顺序稳定。
  //
  // 竞态修复：发送后尚未持久化的临时消息 id 为负值（handleSend 分配大负数），
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
      h =>
        h.id >= 0 &&
        h.session_id === m.session_id &&
        h.turn_num === m.turn_num &&
        h.role === m.role &&
        h.content === m.content
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

  // —— 草稿水合 ——
  // 读取该会话在 IndexedDB 中的未完成草稿轮（error/stop/HITL-reject 等未落库的轮次，
  // 以及服务端尚未把 onDone 结果写回时正在流式生成的轮次）。
  //
  // 每条草稿消息保留其「本地负临时 id」与「正 turn_num」。由于正 turn_num 与实时消息
  // 同号，草稿行会自然排在同轮次已提交消息之后（见末段排序），不会像旧的负轮次方案
  // 那样飘到已提交轮之前。且按 (session, turn, role, content) 与服务端/本地集合精确
  // 匹配——若同一逻辑消息已被服务端落库（serverRowFor 命中）或已存在于本地集合，
  // 则跳过该草稿行，避免同轮次内草稿与实时消息渲染重复。
  const drafts = await readDraftTurns(sessionId);
  //
  // 草稿「陈旧轮」判定：若同一 turn_num 在合并集合中已存在「服务端落库的正 id 行」，
  // 且其中包含一条非流式中的最终 AI 结果（role=ai）——则说明该轮次已被服务端成功持久化，
  // 这段草稿是该轮次被打断时的陈旧残留（例如 Test：手动 kill 后端让草稿保留「回复失败」标记，
  // 随后后端自愈又把同一轮真实内容写回）。此时按 (turn, role, content) 逐条精确匹配会因
  // 内容不同（失败标记 vs 真实回复）而漏过，导致失败标记/失败工具行与服务端真实行一同渲染，
  // 造成同轮重复显示。正确做法：凡是该轮已落库了最终 AI 结果的草稿轮，整体跳过、不再水合。
  //
  // 仅以 role=ai 的服务端行为「已持久化最终结果」的锚点，是因为正常进行中的流式轮次服务端
  // 只先写 human（正 id），AI 结果尚未落库，此时草稿 AI 行仍应水合渲染；只有 AI 已落库
  // 才意味着该轮已实质完成、草稿必属陈旧。
  const staleDraftTurns = new Set<number>();
  for (const m of mergedById.values()) {
    if (m.id >= 0 && m.role === 'ai') staleDraftTurns.add(m.turn_num);
  }
  for (const draft of drafts) {
    for (const dm of draft.messages) {
      if (dm.session_id !== sessionId) continue; // 防御：只水合本会话
      // 陈旧轮：该轮已由服务端持久化最终 AI 结果，整体丢弃草稿行
      if (staleDraftTurns.has(dm.turn_num)) continue;
      // 草稿行在本地集合 / 服务端历史中是否已存在（按逻辑键匹配）
      const alreadyLocal = [...mergedById.values()].some(
        m => m.turn_num === dm.turn_num && m.role === dm.role && m.content === dm.content
      );
      if (alreadyLocal) continue;
      mergedById.set(dm.id, dm);
    }
  }

  // 按 turn_num 升序排序；同轮次内按 id 升序（与后端 messages 表
  // "ORDER BY turn_num ASC, id ASC" 一致）。此前用 id 降序会把同一轮次内
  // （用户消息 + AI 回复共享同一 turn_num）的插入顺序颠倒，导致刷新后
  // AI 回复跑到用户消息上面、最后一条 AI 回复不在最底部。
  //
  // 草稿行使用与实时消息相同的正 turn_num + 负临时 id：对已提交轮次，草稿 id 为负、
  // 该轮真实消息 id 为正，同轮内按 id 升序（负 < 正）草稿排前者 —— 但同一逻辑消息
  // 已被上方「跳过」逻辑剔除，能被水合进来的草稿都是尚未落库的失败轮次，因此不会
  // 与实际渲染冲突。
  chatMessages.value = [...mergedById.values()].sort((a, b) => a.turn_num - b.turn_num || a.id - b.id);
};

/** 是否处于 AI 回复生成中 */
const isSending = ref(false);
/** 当前进行中的流式请求控制器（用于停止生成） */
let activeAgentController: AbortController | null = null;
/**
 * 自增 id 计数器（用于本地临时消息，避免与真实 id 冲突）。
 *
 * 从很大的负数开始、按创建顺序「递增」分配：-1000000、-999999、-999998 …
 * 这样同一轮次（turn_num 相同）内的消息按 id 升序排序时，
 * 恰好等于它们被创建的先后顺序（用户消息最先、AI/工具分段随后），
 * 与后端 "ORDER BY turn_num ASC, id ASC"（用户先写、id 更小）保持一致。
 *
 * 注意：不能像之前那样用 `--tempIdCounter`（递减），否则后创建的 AI/工具
 * 消息 id 反而更小，流式中途切走再切回触发重新排序后，AI 会跑到用户上方。
 */
let tempIdCounter = -1000000;

/**
 * 删除会话时的流式中止处理：
 *
 * 当该会话被删除（home/index.vue 广播 `SESSION_ABORT_STREAM_EVENT`），
 * 若本实例正是该会话（按 sid 匹配）且仍在流式生成，则中止其 AbortController。
 * 对「非激活但被 KeepAlive 缓存且流未中止」的会话尤为关键 —— 删除后若不中止，
 * 后端会继续向已删除会话的 WebSocket 推块，导致已删除的聊天状态被污染。
 *
 * 注意：`activeAgentController` 是 setup 闭包变量，故 handler 须在此作用域内定义，
 * 并以首个入参（会话 id）与本实例 `sessionId` 比对，确保只中止本方会话。
 */
const handleAbortStreamOnDelete = (deletedSid: unknown) => {
  // 用冻结的本实例 `mySid` 比对，而非直播的 `sessionId`：后者读全局 route，
  // 在实例被 KeepAlive 缓存（切到其它会话）时会变成别人的 sid，导致本会话被删时比对不中、
  // 后台流无法中止。
  if (deletedSid !== mySid) return;
  if (activeAgentController) {
    activeAgentController.abort();
    activeAgentController = null;
    isSending.value = false;
  }
  // 会话已删除：清空本实例在 KeepAlive 缓存槽中残留的历史缓存/草稿浏览态
  // （删除非激活会话时槽位未必立即释放，随槽驻留的历史必须主动清除，
  //   使历史严格跟随会话删除，避免手动重访该 sid 时看到已删除会话的残留）。
  chatInputBoxRef.value?.clearHistory?.();
  // 会话已删除：清除本会话在 IndexedDB 中的全部进行中草稿轮，防止孤儿草稿
  // 在重建同 id 会话后错误重水合（Draft 表仍带原会话已删除内容）。
  void clearDraftSession(mySid);
};

/** HITL 审批请求（当 agent 暂停等待人工审批时设置） */
const hitlRequest = ref<HitlRequestData | null>(null);

/** 处理 HITL 审批请求：显示审批弹窗 */
const handleHitlRequest = (data: HitlRequestData) => {
  hitlRequest.value = data;
};

/** 正在进行中的 HITL resume 控制器（single-flight：同一会话只允许一个在跑） */
let activeHitlController: { closed: boolean; abort: () => void } | null = null;

/**
 * 用户审批/拒绝 HITL 请求。
 *
 * 决策不再依赖「实时发送消息时挂载在 `streamChatMessage` 返回的 closure 上的
 * `sendHitlResponse`」——那条闭包只在 `!done && socket.readyState === OPEN` 时可用，
 * 页面刷新/切换会话/浏览器重开后 socket 关闭、controller 为 null，审批会静默 no-op。
 * 这里改为独立 `resumeHitl`：直接新开一条 WS 到后端 `/sessions/agent/ws`，
 * 发送 `hitl_response` 帧即可从 LangGraph checkpoint 流式恢复 agent，从而
 * 支持三层持久化（切 session、刷新、浏览器重开）后仍然可完成审批。
 */
const handleHitlDecision = (decision: 'approve' | 'reject', message: string = '') => {
  const sid = sessionId.value;
  if (!sid) {
    hitlRequest.value = null;
    return;
  }
  // single-flight 只用于「同一待审批项」防重复提交，绝不能静默丢弃新决策。
  // 顺序 HITL（连续多个危险工具依次需要审批）时，上一个 resume 的 WS 仍处于
  // 流式恢复中（closed=false），此时若直接 return 会导致后续点「批准/撤回」完全无反应。
  // 正确做法：先中止/释放上一个仍在跑的 controller 槽位，再为本决策新开一条
  // resume WS——保证每次点击都必然有一条真实通道送出 hitl_response，绝无静默 no-op。
  const stale = activeHitlController && !activeHitlController.closed;
  if (stale) {
    // 中止旧链路的流式恢复（其 abort 会向后端发 {type:'stop'}），并释放其槽位，
    // 避免它一旦在稍后 resolve 时把已换新的 activeHitlController 误清空。
    activeHitlController.abort();
    activeHitlController = null;
  }

  // 记录本次审批的轮次：resume 产出的新消息落到「当前最大轮次 + 1」
  const turnNum = chatMessages.value.reduce((max, m) => Math.max(max, m.turn_num), 0) + 1;

  // 登记本次 resume 轮次的草稿（与 handleSend 一致），使 appendStreamChunk 能实时落盘；
  // resume 流正常结束时对账移除，拒绝/失败时保留草稿缓存失败阶段内容。
  trackDraftTurn(sid, turnNum);

  const onChunk = (
    content: string,
    type: AgentChunkType,
    _sessionId: string,
    meta?: { tool_id?: string; tool_name?: string; args?: Record<string, unknown>; error?: boolean }
  ) => {
    appendStreamChunk(sid, content, type, turnNum, meta);
  };

  const { controller, promise } = resumeHitl(sid, decision, message, onChunk, handleHitlRequest);
  activeHitlController = controller;

  // 拒绝：该工具不会被执行，后端不会回发 tool_end，因此把当前仍处于 running 的
  // 工具卡片标记为 failed（UI 由转圈 spinner 变为红色 ✗），避免永久停在加载中。
  if (decision === 'reject') {
    markRunningToolsFailed();
    // 拒绝不会触发后端回包，立即把含 failed 状态的草稿落盘，保证刷新后失败进度可见
    void writeDraftTurn(sid, turnNum);
  }

  /**
   * 清理本次 HITL 审批链路的悬挂状态。
   *
   * 关键：HITL interrupt 发生时，后端**不关闭**原始生成流的 WebSocket（等待 resume），
   * 因此 `handleSend` 里 `postAgentStream` 返回的 promise 永久挂起，其 `onDone` 永不触发，
   * `isSending` 停留在 `true`。此处审批完成后必须手动复位，否则输入框/生成按钮被永久锁死。
   */
  const finish = () => {
    if (activeHitlController === controller) activeHitlController = null;
    // 原始生成流已废弃：释放其 controller 槽位并复位发送状态
    activeAgentController = null;
    isSending.value = false;
    // 若审批期间没有再触发新的 hitl_request，则关闭审批卡
    if (hitlRequest.value) {
      hitlRequest.value = null;
    }
  };
  promise
    .then(() => {
      // 正常完成：先落最终草稿再对账移除（与 handleSend onDone 一致）
      return commitDraftTurn(sid, turnNum).then(() => {
        untrackDraftTurn(sid, turnNum);
        finish();
        void loadSessionHistory(sid);
      });
    })
    .catch(() => {
      // 出错时同样清理，保持输入可用；卡片的关闭由其他流程决定
      if (activeHitlController === controller) activeHitlController = null;
      activeAgentController = null;
      // HITL resume 失败：进行中的工具未正常完成，标记为 failed（红 ✗）
      markRunningToolsFailed();
      // 保留草稿：把失败前已完成阶段缓存下来
      void writeDraftTurn(sid, turnNum);
      isSending.value = false;
    });

  // 已响应本次审批，收起卡片（若 resume 中 agent 再次暂停会重新弹出）
  hitlRequest.value = null;
};

/**
 * 尝试恢复「待审批」的 HITL 中断卡。
 *
 * 三层持久化场景（会话切换 / 页面刷新 / 浏览器重开 / 服务重启）下，`hitlRequest`
 * 仅存于组件内存，重进会话时是空的。这里从后端 `/get_pending_interrupt`
 * （从 LangGraph checkpoint 重推）查询该会话是否存在未决审批；存在则重新弹出卡片，
 * 供用户再次批准/拒绝。
 */
const restorePendingHitl = async (sid: string) => {
  if (!sid) return;
  // 已有审批在进行或已有卡片则不重复拉起
  if (hitlRequest.value || (activeHitlController && !activeHitlController.closed)) return;
  const pending = await getPendingInterrupt(sid);
  if (pending && typeof pending === 'object' && !Array.isArray(pending) && typeof pending.tool_name === 'string') {
    hitlRequest.value = {
      tool_name: pending.tool_name,
      tool_args: pending.tool_args ?? {},
      description: pending.description ?? '',
      allowed_decisions: pending.allowed_decisions ?? []
    };
  }
};

/** 停止当前 AI 回复生成（前端本地中止 + 通知后端止停） */
const handleStop = () => {
  activeAgentController?.abort();
  activeAgentController = null;
  // 若正在 HITL resume 流式恢复中，同样中止该 controller
  // （其 abort 会向后端发 {type:'stop'}，令 answering=False 触发 CancelledError）
  activeHitlController?.abort();
  activeHitlController = null;
  // 中止的回合未完成，把进行中的工具卡片标记为 failed（红 ✗）
  markRunningToolsFailed();
  // 中止同样属于「未完成轮」：为当前会话所有活动草稿轮各落一版含失败状态的快照，
  // 保证停止后刷新仍能看到已产出的寒暄/分析/前置工具阶段，而非整轮消失。
  const sid = sessionId.value || 'default';
  for (const turnNum of [...activeDraftTurns]) {
    void writeDraftTurn(sid, turnNum);
  }
  // 中止后待审批卡已被本次审批处理过，无需重复展示
  hitlRequest.value = null;
  isSending.value = false;
};

/** 输入框草稿（受控，经 defineModel 双向绑定到 inputBox.vue） */
const draft = ref('');

/**
 * 输入框组件实例引用：会话被删除（前端广播 `SESSION_ABORT_STREAM_EVENT`）时，
 * 调用其 `clearHistory()` 清空本会话在 KeepAlive 缓存槽中残留的历史缓存，
 * 使历史严格跟随会话删除而清除（即使该槽位尚未被 LRU 淘汰）。
 */
const chatInputBoxRef = useTemplateRef<InstanceType<typeof ChatInputBox>>('chatInputBoxRef');

/**
 * 将一段流式 chunk 按语义类型合并进 `chatMessages`（单一数据源）。
 *
 * 该函数是 `handleSend`（普通对话）与「HITL resume」两条路径共用的消息渲染逻辑：
 * - text: 若同轮次末位是 AI 消息则追加，否则（末位是 TOOL / 跨轮次）新建 AI 消息
 * - tool_start: 新建一条 TOOL 消息（status=running）
 * - tool_end: 将最近一条同轮次 TOOL 消息标记为 done
 * - tool_result: 将最近一条同轮次 TOOL 消息填充参数与结果文本，并按 error 标记状态
 *
 * 通过 `turnNum` 限定范围，避免把与当前流式回合无关的历史消息误当目标。
 *
 * @param sid 会话 id
 * @param content chunk 文本（text 时为正文，tool_start 时为工具名，tool_result 时为结果文本）
 * @param type 语义类型
 * @param turnNum 本回合轮次号（新消息写入该轮次）
 * @param meta 工具调用元数据（仅 tool_result 时有值：tool_id/tool_name/args/error）
 */
const appendStreamChunk = (
  sid: string,
  content: string,
  type: AgentChunkType,
  turnNum: number,
  meta?: { tool_id?: string; tool_name?: string; args?: Record<string, unknown>; error?: boolean }
) => {
  const last = chatMessages.value[chatMessages.value.length - 1];
  // 判定是否属于一条「活动草稿轮」（发送即 created，onDone/error/stop 后移除）。
  // 命中时在尾部分层写入草稿：文本追加 200ms 去抖，离散 tool/首文本立即落盘。
  const isActiveDraft = isDraftTurnActive(turnNum);
  if (type === 'text') {
    if (last && last.role === CHAT_ROLE.AI && last.turn_num === turnNum) {
      // 同轮次末位是 AI → 追加正文
      last.content += content;
    } else {
      // 末位是 TOOL / 非本回合 → 新建一条 AI 消息承载
      chatMessages.value.push({
        session_id: sid,
        role: CHAT_ROLE.AI,
        content,
        id: tempIdCounter++,
        turn_num: turnNum,
        timestamp: new Date().toISOString()
      });
    }
    if (isActiveDraft) scheduleDraftWrite(sid, turnNum);
  } else if (type === 'reasoning') {
    // 模型思考块：同轮次末位 AI 消息上的 reasoning 字段逐块拼接，不干扰正文累积。
    // 末位是 TOOL/非本回合时新建一条 AI 占位消息承载（正文可能稍后才到）。
    let target: MessageItem;
    if (last && last.role === CHAT_ROLE.AI && last.turn_num === turnNum) {
      target = last;
    } else {
      target = {
        session_id: sid,
        role: CHAT_ROLE.AI,
        content: '',
        id: tempIdCounter++,
        turn_num: turnNum,
        timestamp: new Date().toISOString()
      };
      chatMessages.value.push(target);
    }
    target.reasoning = (target.reasoning ?? '') + content;
    // 思考块是离散阶段，去抖直觉上可用，但思考内容需随流实时落盘以支持刷新恢复，
    // 与正文共用文本追加的去抖路径即可（思考块通常不会如正文那般高频细分）。
    if (isActiveDraft) scheduleDraftWrite(sid, turnNum);
  } else if (type === 'tool_start') {
    chatMessages.value.push({
      session_id: sid,
      role: CHAT_ROLE.TOOL,
      content: '',
      toolName: content,
      toolStatus: 'running',
      // 参数在 tool_start 时即随 meta 下发，执行中即可实时查看调用参数
      toolArgs: meta?.args ?? undefined,
      id: tempIdCounter++,
      turn_num: turnNum,
      timestamp: new Date().toISOString()
    });
    if (isActiveDraft) void commitDraftTurn(sid, turnNum);
  } else if (type === 'tool_end') {
    // 标记最近一条本回合 TOOL 消息为已完成
    for (let i = chatMessages.value.length - 1; i >= 0; i--) {
      const row = chatMessages.value[i];
      if (row.role === CHAT_ROLE.TOOL && row.turn_num === turnNum) {
        row.toolStatus = 'done';
        break;
      }
    }
    if (isActiveDraft) void commitDraftTurn(sid, turnNum);
  } else if (type === 'tool_result') {
    // 填充最近一条本回合 TOOL 消息的参数与结果文本，并按 error 标记状态
    for (let i = chatMessages.value.length - 1; i >= 0; i--) {
      const row = chatMessages.value[i];
      if (row.role === CHAT_ROLE.TOOL && row.turn_num === turnNum) {
        if (meta?.tool_name) row.toolName = meta.tool_name;
        if (meta?.args) row.toolArgs = meta.args;
        row.toolResult = content;
        row.toolStatus = meta?.error ? 'error' : 'done';
        break;
      }
    }
    // tool_result 是离散阶段，立即落盘（无论成功或 error 都保留前置内容）
    if (isActiveDraft) void commitDraftTurn(sid, turnNum);
  }
  // 触发响应式更新
  chatMessages.value = [...chatMessages.value];
};

/**
 * 把当前仍处于 running 的工具卡片统一标记为 failed（UI 转红色 ✗）。
 *
 * 用于「工具调用未正常完成」的所有路径：HITL 拒绝、用户中止、流式错误。
 * 这些场景下后端都不会回发对应的 tool_end，卡片若不标记会永远停在转圈。
 */
const markRunningToolsFailed = () => {
  let changed = false;
  chatMessages.value = chatMessages.value.map(m => {
    if (m.role === CHAT_ROLE.TOOL && m.toolStatus === 'running') {
      changed = true;
      return { ...m, toolStatus: 'failed' as const };
    }
    return m;
  });
  if (!changed) chatMessages.value = [...chatMessages.value];
};

/**
 * 进行中草稿的离散写频控制。
 *
 * 关键设计：草稿消息**沿用其所在的真实 `turn_num`**（与 `handleSend`/HITL resume 为消息
 * 分配的正轮次一致），而非独立的负草稿轮次。原因有二：
 *
 * 1. **排序自然**：`loadSessionHistory` 按 `turn_num` 升序排序。草稿沿用真实 turn_num
 *    即出现在其逻辑位置（in-flight/error 轮次之后紧挨的正确轮次），无需特殊处理。
 * 2. **对账去重可行**：服务端只有在该轮 agent 完整成功返回时（`aafter_agent`）才会落库，
 *    in-flight / error 的轮在服务端**完全没有行**，故草稿沿用该轮 turn_num 不会与已落库
 *    消息重号；而对账时 `serverRowFor` 按「同 session + 同 turn_num + 同 role + 同 content」
 *    精确匹配，恰好能用服务端正 id 行替换本地草稿的负临时 id 行，实现自然去重。
 *
 * 因此 `drafts` 表以 `[session_id + turn_num]` 为主键，同一轮反复覆盖写即「每步缓存」。
 */

/**
 * 将当前 `chatMessages` 中属于指定轮次的全部消息，整体持久化为一张本地草稿。
 *
 * 仅保存 `turn_num === turnNum` 的消息，避免覆盖到与本次发送/本次 resume 无关的轮次。
 *
 * @param sid      会话 id
 * @param turnNum  本轮次（流回调使用的真实 turn_num）
 */
const writeDraftTurn = async (sid: string, turnNum: number) => {
  const rows = chatMessages.value.filter(m => m.turn_num === turnNum);
  if (rows.length === 0) return; // 本轮还没有任何消息，无需写空草稿
  // 深拷贝：chatMessages 是 Vue ref，元素经 ref 解包后为响应式 Proxy。
  // 若仅浅展开/局部深拷，嵌套的 images/audios/videos/toolArgs 仍是 Proxy 引用，
  // Dexie put() 走 IndexedDB 结构化克隆时会抛 DataCloneError → 草稿写入失败。
  // MessageItem 仅含 JSON 兼容字段（无 Date/Function/Blob），整体 JSON 往返深拷贝最稳妥，
  // 同时避免后续流式修改污染已落盘草稿。
  const snapshot = rows.map(m => JSON.parse(JSON.stringify(m)));
  try {
    await saveDraftTurn({ session_id: sid, turn_num: turnNum, messages: snapshot });
  } catch (e) {
    console.warn('[writeDraftTurn] 草稿写入失败：', sid, turnNum, e);
  }
};

/** 文本追加草稿的 200ms 去抖计时器（key: `${sid}:${turnNum}`） */
const draftDebounceTimers = new Map<string, ReturnType<typeof setTimeout>>();

/**
 * 排定一次「文本追加」的草稿写入（200ms trailing 去抖）。
 *
 * 高频率文本 chunk 不逐条落盘，改为去抖合并；离散阶段（send / tool 各阶段 / error /
 * 首个文本 / 服务端完成）则由调用方走 `commitDraftTurn` 立即写入。
 */
const scheduleDraftWrite = (sid: string, turnNum: number) => {
  const key = `${sid}:${turnNum}`;
  const existing = draftDebounceTimers.get(key);
  if (existing) clearTimeout(existing);
  draftDebounceTimers.set(
    key,
    setTimeout(() => {
      draftDebounceTimers.delete(key);
      void writeDraftTurn(sid, turnNum);
    }, 200)
  );
};

/**
 * 立即写入草稿（离散阶段调用）并取消该轮未决的文本去抖计时。
 * 若该轮文字追加仍在排程中，先落一次当前快照再清除计时，避免重复写。
 */
const commitDraftTurn = async (sid: string, turnNum: number) => {
  const key = `${sid}:${turnNum}`;
  const pending = draftDebounceTimers.get(key);
  if (pending) {
    clearTimeout(pending);
    draftDebounceTimers.delete(key);
  }
  await writeDraftTurn(sid, turnNum);
};

/**
 * 清除某轮草稿并取消其未决去抖计时（服务端成功落库对账 / 清空会话时调用）。
 */
const removeDraftTurn = (sid: string, turnNum: number) => {
  const key = `${sid}:${turnNum}`;
  const pending = draftDebounceTimers.get(key);
  if (pending) {
    clearTimeout(pending);
    draftDebounceTimers.delete(key);
  }
  void clearDraftTurn(sid, turnNum);
};

/**
 * 正在进行流式生成的「活动轮次」集合（元素为真实 turn_num）。
 *
 * `handleSend` 与「HITL resume」发起流式时各 push 一个 turn_num；流正常结束/报错/中止/
 * 拒绝时移除。`appendStreamChunk` 只有在 turn_num 属于该集合时才写草稿，避免对历史行
 * 误触发写盘。
 */
const activeDraftTurns = new Set<number>();

/** 登记一条活动的草稿轮次（发送时创建，完成后移除）。 */
const trackDraftTurn = (sid: string, turnNum: number) => {
  activeDraftTurns.add(turnNum);
  // 首次创建即写一张草稿，保证「发送即缓存」的最快帧（用户消息 + 空 AI 占位）。
  void writeDraftTurn(sid, turnNum);
};

/** 判定某轮是否处于活动草稿写盘态。 */
const isDraftTurnActive = (turnNum: number): boolean => activeDraftTurns.has(turnNum);

/**
 * 移除某轮次的草稿登记。服务端成功落库后调用：清除草稿 + 取消未决去抖计时，
 * 由调用方随后触发 `loadSessionHistory` 用服务端正 id 行替换本地负临时 id 行。
 */
const untrackDraftTurn = (sid: string, turnNum: number) => {
  const had = activeDraftTurns.delete(turnNum);
  removeDraftTurn(sid, turnNum);
  void had; // keep ref for clarity
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
  const sid = sessionId.value || 'default';

  // 计算下一轮次号：取当前消息中最大 turn_num + 1，而非按数组长度。
  const turnNum = chatMessages.value.reduce((max, m) => Math.max(max, m.turn_num), 0) + 1;

  // 携带本次待发送的图片（发送时取走并清空待发送列表）
  const imageBase64List = selectedImages.value.map(img => img.base64);
  // 携带本次待发送的音频/视频（发送时取走并清空待发送列表）
  const audioBytesList = selectedAudios.value.map(a => a.base64);
  const videoBytesList = selectedVideos.value.map(v => v.base64);

  // 追加用户消息（本地即时显示）
  const userMsg: MessageItem = {
    session_id: sid,
    role: CHAT_ROLE.USER,
    content: text,
    images: imageBase64List,
    audios: audioBytesList,
    videos: videoBytesList,
    id: tempIdCounter++,
    turn_num: turnNum,
    timestamp: new Date().toISOString()
  };

  // 初始 AI 占位消息（内容随流式块逐步填充）
  const aiMsg: MessageItem = {
    session_id: sid,
    role: CHAT_ROLE.AI,
    content: '',
    id: tempIdCounter++,
    turn_num: turnNum,
    timestamp: new Date().toISOString()
  };

  chatMessages.value = [...chatMessages.value, userMsg, aiMsg];

  // 发送后清空待发送图片/音频/视频与输入区
  selectedImages.value = [];
  selectedAudios.value = [];
  selectedVideos.value = [];
  draft.value = '';

  isSending.value = true;

  // 登记本轮为「活动草稿轮次」，使 appendStreamChunk 能据此落盘；
  // 首次登记即写一版「发送即缓存」帧（用户消息 + 空 AI 占位）。
  // 在流正常完成（onDone）时移除；报错/中止/拒绝时保留草稿缓存失败阶段内容。
  trackDraftTurn(sid, turnNum);

  /**
   * 流式 chunk 回调：复用共享的 `appendStreamChunk` 按语义类型动态管理消息分段
   * （text/tool_start/tool_end），与 HITL resume 路径共用同一套渲染逻辑。
   */
  const onStreamChunk = (
    content: string,
    type: AgentChunkType,
    _sessionId: string,
    meta?: { tool_id?: string; tool_name?: string; args?: Record<string, unknown>; error?: boolean }
  ) => {
    appendStreamChunk(sid, content, type, turnNum, meta);
  };

  try {
    const req: MultiModalMessage = { text };
    if (imageBase64List.length > 0) req.image_base64_list = imageBase64List;
    if (audioBytesList.length > 0) req.audio_bytes_list = audioBytesList;
    if (videoBytesList.length > 0) req.video_bytes_list = videoBytesList;
    activeAgentController = postAgentStream(
      sid,
      req,
      onStreamChunk,
      (meta) => {
        // 流正常结束：先落一版最终草稿（防止最后一刻的文本变更未被去抖写入），
        // 再移除当前轮草稿登记并触发历史对账——服务端此时已将本轮落库为正 turn_num 消息，
        // loadSessionHistory 会用端正 id 替换本地的负临时 id，从而去重。
        // 同时把 done 帧携带的模型元数据（modelName/inputTokens/outputTokens）挂到本轮 AI 消息上。
        if (meta) {
          const ai = chatMessages.value.find(
            m => m.role === CHAT_ROLE.AI && m.turn_num === turnNum
          );
          if (ai) {
            if (meta.modelName !== undefined) ai.modelName = meta.modelName;
            if (meta.inputTokens !== undefined) ai.inputTokens = meta.inputTokens;
            if (meta.outputTokens !== undefined) ai.outputTokens = meta.outputTokens;
            chatMessages.value = [...chatMessages.value];
          }
        }
        void commitDraftTurn(sid, turnNum).then(() => {
          untrackDraftTurn(sid, turnNum);
          activeAgentController = null;
          isSending.value = false;
          void loadSessionHistory(sid);
        });
      },
      err => {
        // 流式出错：进行中的工具调用未正常完成，标记为 failed（红 ✗）。
        // 草稿**保留**——把已完成的寒暄/分析/前置工具阶段内容缓存下来，
        // 不因最终结果未输出而丢失，用户刷新后仍能看见失败前的进度。
        activeAgentController = null;
        aiMsg.content = t('errors.replyFailed', { reason: String(err) });
        markRunningToolsFailed();
        // 落一版含失败状态的草稿快照
        void writeDraftTurn(sid, turnNum);
        isSending.value = false;
      },
      handleHitlRequest
    );
  } catch (e) {
    // 同步抛错（罕见），此时流未启动，直接解锁。
    // 同样保留草稿：用户消息 + 空 AI 占位 + 失败提示均已缓存。
    activeAgentController = null;
    aiMsg.content = t('errors.sendFailed', { reason: String(e) });
    void writeDraftTurn(sid, turnNum);
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

/**
 * 已选音频（base64 形式，随消息发送）。
 * 仅在「本地预览 + 发送」期间存在，发送/取消后清空。
 */
const selectedAudios = ref<{ base64: string; name: string }[]>([]);

/** 单条消息允许附带的最大音频数量 */
const MAX_SELECTED_AUDIOS = 5;

/** 隐藏的音频文件选择框 */
const audioFileInput = useTemplateRef<HTMLInputElement>('audioFileInputRef');

/** 读取音频文件为 DataURL（含 data:audio/...;base64 前缀，需剥离前缀再发送） */
const readAudioFile = (file: File): Promise<string> =>
  new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });

/** 移除一个已选音频 */
const removeAudio = (index: number) => {
  selectedAudios.value.splice(index, 1);
  selectedAudios.value = [...selectedAudios.value];
};

/** 触发系统音频文件选择 */
const triggerAudioPicker = () => {
  audioFileInput.value?.click();
};

/** 音频选择回调：读取为 base64 并加入待发送列表（上限 MAX_SELECTED_AUDIOS） */
const onAudioSelected = async (event: Event) => {
  const input = event.target as HTMLInputElement;
  // 先拷贝为普通数组副本再重置 input.value。
  // input.files 是「活」的 FileList —— 一旦把 value 置空，浏览器会立即清空该 FileList，
  // 若此后才读取 files 会得到空数组，导致 selectedAudios 永不入库、预览区不显示。
  const files = Array.from(input.files ?? []);
  input.value = ''; // 允许重复选择同一文件
  if (files.length === 0) return;

  // 数量上限：超出的部分直接截断并提示
  const remaining = MAX_SELECTED_AUDIOS - selectedAudios.value.length;
  if (remaining <= 0) {
    alert(t('chatInput.maxAudios', { count: MAX_SELECTED_AUDIOS }));
    return;
  }
  const accepted = files.slice(0, remaining);
  if (files.length > remaining) {
    alert(t('chatInput.maxAudiosExceed', { count: MAX_SELECTED_AUDIOS, extra: files.length - remaining }));
  }

  for (const file of accepted) {
    if (!file.type.startsWith('audio/')) continue;
    try {
      const dataUrl = await readAudioFile(file);
      // data:audio/mpeg;base64,xxxxx -> 仅保留 base64 部分
      const base64 = dataUrl.split(',')[1] ?? '';
      selectedAudios.value.push({ base64, name: file.name });
    } catch (e) {
      console.warn('[onAudioSelected] 读取音频失败：', file.name, e);
    }
  }
  selectedAudios.value = [...selectedAudios.value];
};

/**
 * 已选视频（base64 形式，随消息发送）。
 * 仅在「本地预览 + 发送」期间存在，发送/取消后清空。
 */
const selectedVideos = ref<{ base64: string; name: string }[]>([]);

/** 单条消息允许附带的最大视频数量 */
const MAX_SELECTED_VIDEOS = 3;

/** 隐藏的视频文件选择框 */
const videoFileInput = useTemplateRef<HTMLInputElement>('videoFileInputRef');

/** 读取视频文件为 DataURL（含 data:video/...;base64 前缀，需剥离前缀再发送） */
const readVideoFile = (file: File): Promise<string> =>
  new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });

/** 移除一个已选视频 */
const removeVideo = (index: number) => {
  selectedVideos.value.splice(index, 1);
  selectedVideos.value = [...selectedVideos.value];
};

/** 触发系统视频文件选择 */
const triggerVideoPicker = () => {
  videoFileInput.value?.click();
};

/** 视频选择回调：读取为 base64 并加入待发送列表（上限 MAX_SELECTED_VIDEOS） */
const onVideoSelected = async (event: Event) => {
  const input = event.target as HTMLInputElement;
  // 先拷贝为普通数组副本再重置 input.value（Safari 异步处理，见 onImageSelected）。
  const files = Array.from(input.files ?? []);
  input.value = ''; // 允许重复选择同一文件
  if (files.length === 0) return;

  // 数量上限：超出的部分直接截断并提示
  const remaining = MAX_SELECTED_VIDEOS - selectedVideos.value.length;
  if (remaining <= 0) {
    alert(t('chatInput.maxVideos', { count: MAX_SELECTED_VIDEOS }));
    return;
  }
  const accepted = files.slice(0, remaining);
  if (files.length > remaining) {
    alert(t('chatInput.maxVideosExceed', { count: MAX_SELECTED_VIDEOS, extra: files.length - remaining }));
  }

  for (const file of accepted) {
    if (!file.type.startsWith('video/')) continue;
    try {
      const dataUrl = await readVideoFile(file);
      // data:video/mp4;base64,xxxxx -> 仅保留 base64 部分
      const base64 = dataUrl.split(',')[1] ?? '';
      selectedVideos.value.push({ base64, name: file.name });
    } catch (e) {
      console.warn('[onVideoSelected] 读取视频失败：', file.name, e);
    }
  }
  selectedVideos.value = [...selectedVideos.value];
};

/** 工具触发 */
const handleOperate = (type: string, event: string) => {
  if (!event || !type) return;
  // 工具栏
  switch (event) {
    case 'createSession':
      handleCreateSession();
      return;
    case 'uploadImage':
      triggerImagePicker();
      return;
    case 'uploadAudio':
      triggerAudioPicker();
      return;
    case 'uploadVideo':
      triggerVideoPicker();
      return;
    default:
      return;
  }
};

/** 新增会话：生成随机 session_id，创建新会话窗口并切换 */
const handleCreateSession = () => {
  const newSessionId = crypto.randomUUID();
  router.push({ name: 'home-sid', params: { sid: newSessionId } });
  // 新会话：立即用当前全局 profile 创建并锁定角色快照，保证头像/名字正确显示
  ensureSessionCharacter(newSessionId);
  // 持久化占位会话（与 home/index.vue 行为一致），保证工具栏/空态新建后刷新仍保留
  const now = new Date();
  const pad = (n: number) => String(n).padStart(2, '0');
  const createTime = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} ${pad(now.getHours())}:${pad(now.getMinutes())}`;
  cacheSessionMeta({ id: newSessionId, title: t('history.newSession'), createTime, updatedAt: Date.now() });
};

/** 为本实例加载指定会话的历史（清空本地状态后重建）。仅供新会话/异常兜底路径调用。 */
const doLoadFor = (sid: string) => {
  // 切换/首载新 sid 时，先清空所有会话语义下的本地状态，再加载本会话自己的历史。
  // 若切换前 chatMessages 还留着上个会话的消息，即便用 loadSessionHistory 按 id 去重
  // merge，新旧会话消息也会混在一起，导致「切到新会话却显示旧会话内容」。
  chatMessages.value = [];
  isSending.value = false;
  // 放弃上个会话残留的进行中请求控制器（其流已随会话切换而失效/后继不可用）
  activeAgentController = null;
  activeHitlController?.abort();
  activeHitlController = null;
  // 上个会话的待审批卡 / 输入草稿 / 已选图片都不应带到新会话
  hitlRequest.value = null;
  draft.value = '';
  selectedImages.value = [];
  selectedAudios.value = [];
  selectedVideos.value = [];
  // 加载该会话已锁定的角色快照（无快照则用全局 profile 锁定）
  ensureSessionCharacter(sid);
  loadSessionHistory(sid);
  // 恢复三层持久化场景下可能仍待审批的 HITL 中断卡（切 session / 刷新 / 重开 / 服务重启）
  restorePendingHitl(sid);
  mySidLoaded = true;
};

// 首屏加载当前会话的历史消息，获取合并后的列表渲染到 ChatBox
watch(
  sessionId,
  sid => {
    if (!sid) return;

    // 1) 首次（尚未为 mySid 加载过历史）：只加载本实例自己的会话历史。
    //    本 `[sid].vue` 实例在创建时已把 sid 冻结为 mySid，只属于这一个会话，
    //    因此首载也只加载 mySid —— 绝不加载任何其它会话的内容。
    //    注意 `immediate: true` 在 setup 同步阶段触发时 isActive 仍为 false（onActivated
    //    尚未跑），故不能依赖 isActive 判断首载，须用 mySidLoaded 兜底。
    if (!mySidLoaded) {
      doLoadFor(mySid);
      return;
    }

    // 2) 本实例已被 KeepAlive 缓存且处于非激活态（用户已切到其它会话）：
    //    `route.params.sid`（全局 route 单例）已变成别人的值，导致本 sessionId 重算成他人 id。
    //    但该实例自己的内存状态（chatMessages/草稿/滚动/流式 activeAgentController/HITL 卡）
    //    必须原样保留，等切回时恢复 —— 直接早退，绝不执行破坏性清空。否则切走再切回时
    //    对话框/流式状态会全被清掉（对话框无了 / 后台流被中止）。
    if (!isActive.value) return;

    // 3) 激活且已加载过、且切回的就是本会话（sessionId 变回 mySid）：
    //    原样恢复内存态，不重复清空加载，自然保住在后台继续流式的消息、草稿与滚动位置。
    //    —— 加固：若内存态被污染（存在其它会话的消息，例如此前被（4）误写过）或为空，
    //    则重新加载本会话历史，保证切回展示的一定是本会话自己的内容。否则只做幂等刷新。
    if (sid === mySid) {
      const polluted = chatMessages.value.some(m => m.session_id && m.session_id !== mySid);
      if (chatMessages.value.length === 0 || polluted) {
        doLoadFor(mySid);
      } else {
        ensureSessionCharacter(mySid);
        restorePendingHitl(mySid);
      }
      return;
    }

    // 4) 激活但 sessionId 不是本会话：这通常是「切走瞬间」的 watcher 先于
    //    onDeactivated 触发（route 已变成别人、isActive 尚未置 false）。
    //    绝不能把它当成新会话去 `doLoadFor(sid)` —— 那样会把别人的消息写进本实例
    //    的内存态，导致切回时（3）展示出错误的会话内容。此处应原样保留本地状态，
    //    等 onDeactivated 置 isActive=false；切回时由（3）负责恢复/兜底重载。
    return;
  },
  { immediate: true }
);

// 挂载后确保角色信息已加载（KeepAlive 恢复时 immediate 已触发过，此处兜底）
onMounted(() => {
  if (sessionId.value) {
    ensureSessionCharacter(sessionId.value);
  }
  // 订阅「删除会话 → 中止流式生成」事件。
  // 该会话可能处于非激活状态仍被 KeepAlive 缓存且流未中止；删除时由
  // home/index.vue 广播，这里据此中止本实例的 AbortController。
  on(SESSION_ABORT_STREAM_EVENT, handleAbortStreamOnDelete);
});

// 组件卸载（KeepAlive 缓存槽被淘汰/销毁）时移除监听，避免泄漏
onUnmounted(() => {
  off(SESSION_ABORT_STREAM_EVENT, handleAbortStreamOnDelete);
});
</script>

<i18n lang="json">
{
  "zh": {
    "hitl": {
      "title": "操作需要审批",
      "tool": "工具",
      "description": "描述",
      "reject": "拒绝",
      "approve": "批准"
    },
    "errors": {
      "replyFailed": "（回复失败：{reason}）",
      "sendFailed": "（发送失败：{reason}）"
    }
  },
  "en": {
    "hitl": {
      "title": "Action Requires Approval",
      "tool": "Tool",
      "description": "Description",
      "reject": "Reject",
      "approve": "Approve"
    },
    "errors": {
      "replyFailed": "(Reply failed: {reason})",
      "sendFailed": "(Send failed: {reason})"
    }
  },
  "ja": {
    "hitl": {
      "title": "操作の承認が必要です",
      "tool": "ツール",
      "description": "説明",
      "reject": "拒否",
      "approve": "承認"
    },
    "errors": {
      "replyFailed": "（返信に失敗：{reason}）",
      "sendFailed": "（送信に失敗：{reason}）"
    }
  },
  "ko": {
    "hitl": {
      "title": "작업 승인이 필요합니다",
      "tool": "도구",
      "description": "설명",
      "reject": "거부",
      "approve": "승인"
    },
    "errors": {
      "replyFailed": "（회신 실패：{reason}）",
      "sendFailed": "（전송 실패：{reason}）"
    }
  }
}
</i18n>
