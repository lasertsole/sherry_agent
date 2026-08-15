<template>
  <div class="flex flex-col flex-1 h-full bg-white dark:bg-[#131619]">
    <!-- 聊天主体 / 空态（有会话 sid 时始终显示聊天面板；仅无 sid 的根路径显示"开启新对话"） -->
    <template v-if="sessionId">
      <!-- HITL 审批卡片：非模态，置于历史消息区顶部（不遮最新会话），
           占用独立位置不影响其余消息查看；可随内容自然撑开。 -->
      <div
        v-if="hitlRequest"
        class="shrink-0 mx-2 mt-2 bg-white dark:bg-[#131619] rounded-lg border border-solid border-gray-light dark:border-gray-dark shadow-lg">
        <div class="flex flex-col gap-3 p-3">
          <div class="text-sm font-semibold">{{ t('hitl.title', 'Action Requires Approval') }}</div>
          <div class="text-sm text-gray-500">{{ t('hitl.tool', 'Tool') }}: <span class="font-bold">{{ hitlRequest?.tool_name }}</span></div>
          <div v-if="hitlRequest?.description" class="text-sm whitespace-pre-wrap">{{ hitlRequest.description }}</div>
          <div v-if="hitlRequest?.tool_args && Object.keys(hitlRequest.tool_args).length > 0" class="text-xs bg-gray-50 dark:bg-gray-800 p-3 rounded-lg overflow-auto max-h-40">
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
          </div>
          <!-- 输入框：存在待审批的 HITL 请求时禁止输入/发送，并提示等待审批 -->
          <ChatInputBox
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
    <div v-else class="flex-1 flex flex-col items-center justify-center gap-4">
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
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import { useRoute, useRouter } from 'vue-router';
import type { MessageItem, HitlRequestData } from '../type.ts';
import { CHAT_ROLE } from '../type.ts';
import type { CachedCharacter, CachedMessage } from '@/composables/db';
import { DEFAULT_CACHED_CHARACTER, cacheCharacter, readCachedCharacter, cacheSessionMeta } from '@/composables/db';
import { tools } from '../config';
import { resumeHitl, type AgentChunkType } from '@/composables/bridge';
import { get_history_by_turn_page, getPendingInterrupt, postAgentStream, SESSION_ABORT_STREAM_EVENT } from '@/composables/messages';
import { on, off } from '@/composables/mitt';

// 图片预览
const { openPreview } = useImagePreview();

const { t } = useI18n();
const route = useRoute();
const router = useRouter();

/** 当前会话 ID（来自路由参数 [sid]） */
const sessionId = computed(() => String(route.params.sid ?? ''));

/**
 * 角色显示信息（来源为本地 Dexie 按会话缓存的快照，见 `db.ts` 的 `CachedCharacter`）。
 * - `userAvatar` / `aiAvatar` 为 base64 data URL（用户自定义）或 `/avatar/xxx.jpg` 相对 URL（内置默认），`<img>` 均可直接渲染。
 * - 每次切换/新建会话时从对应会话快照（或全局待定 profile）刷新，旧会话保留各自快照。
 */
const characterInfo = ref<{ userName: string; userAvatar: string; aiName: string; aiAvatar: string }>({
  userName: DEFAULT_CACHED_CHARACTER.userName,
  userAvatar: DEFAULT_CACHED_CHARACTER.userAvatar,
  aiName: DEFAULT_CACHED_CHARACTER.aiName,
  aiAvatar: DEFAULT_CACHED_CHARACTER.aiAvatar,
});

/** 默认角色显示信息（内置：远野汉娜 / 橘雪莉 + 默认头像 URL，见 `defaultCharacter.ts`） */
const defaultCharacter = (): { userName: string; userAvatar: string; aiName: string; aiAvatar: string } => ({
  userName: DEFAULT_CACHED_CHARACTER.userName,
  userAvatar: DEFAULT_CACHED_CHARACTER.userAvatar,
  aiName: DEFAULT_CACHED_CHARACTER.aiName,
  aiAvatar: DEFAULT_CACHED_CHARACTER.aiAvatar,
});

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
        aiAvatar: snap.aiAvatar ?? defaultInfo.aiAvatar,
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
      readCachedCharacter(sessionId),
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
  if (deletedSid !== sessionId.value) return;
  if (activeAgentController) {
    activeAgentController.abort();
    activeAgentController = null;
    isSending.value = false;
  }
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
  // single-flight：同会话已有其他 resume 在跑时直接忽略本次点击
  if (activeHitlController && !activeHitlController.closed) return;

  // 记录本次审批的轮次：resume 产出的新消息落到「当前最大轮次 + 1」
  const turnNum =
    chatMessages.value.reduce((max, m) => Math.max(max, m.turn_num), 0) + 1;

  const onChunk = (content: string, type: AgentChunkType) => {
    appendStreamChunk(sid, content, type, turnNum);
  };

  const { controller, promise } = resumeHitl(sid, decision, message, onChunk, handleHitlRequest);
  activeHitlController = controller;

  // 拒绝：该工具不会被执行，后端不会回发 tool_end，因此把当前仍处于 running 的
  // 工具卡片标记为 failed（UI 由转圈 spinner 变为红色 ✗），避免永久停在加载中。
  if (decision === 'reject') {
    markRunningToolsFailed();
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
  promise.then(finish).catch(() => {
    // 出错时同样清理，保持输入可用；卡片的关闭由其他流程决定
    if (activeHitlController === controller) activeHitlController = null;
    activeAgentController = null;
    // HITL resume 失败：进行中的工具未正常完成，标记为 failed（红 ✗）
    markRunningToolsFailed();
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
  if (
    pending &&
    typeof pending === 'object' &&
    !Array.isArray(pending) &&
    typeof pending.tool_name === 'string'
  ) {
    hitlRequest.value = {
      tool_name: pending.tool_name,
      tool_args: pending.tool_args ?? {},
      description: pending.description ?? '',
      allowed_decisions: pending.allowed_decisions ?? [],
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
  // 中止后待审批卡已被本次审批处理过，无需重复展示
  hitlRequest.value = null;
  isSending.value = false;
};

/** 输入框草稿（受控，经 defineModel 双向绑定到 inputBox.vue） */
const draft = ref('');

/**
 * 将一段流式 chunk 按语义类型合并进 `chatMessages`（单一数据源）。
 *
 * 该函数是 `handleSend`（普通对话）与「HITL resume」两条路径共用的消息渲染逻辑：
 * - text: 若同轮次末位是 AI 消息则追加，否则（末位是 TOOL / 跨轮次）新建 AI 消息
 * - tool_start: 新建一条 TOOL 消息（status=running）
 * - tool_end: 将最近一条同轮次 TOOL 消息标记为 done
 *
 * 通过 `turnNum` 限定范围，避免把与当前流式回合无关的历史消息误当目标。
 *
 * @param sid 会话 id
 * @param content chunk 文本（text 时为正文，tool_start 时为工具名）
 * @param type 语义类型
 * @param turnNum 本回合轮次号（新消息写入该轮次）
 */
const appendStreamChunk = (sid: string, content: string, type: AgentChunkType, turnNum: number) => {
  const last = chatMessages.value[chatMessages.value.length - 1];
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
        timestamp: new Date().toISOString(),
      });
    }
  } else if (type === 'tool_start') {
    chatMessages.value.push({
      session_id: sid,
      role: CHAT_ROLE.TOOL,
      content: '',
      toolName: content,
      toolStatus: 'running',
      id: tempIdCounter++,
      turn_num: turnNum,
      timestamp: new Date().toISOString(),
    });
  } else if (type === 'tool_end') {
    // 标记最近一条本回合 TOOL 消息为已完成
    for (let i = chatMessages.value.length - 1; i >= 0; i--) {
      const row = chatMessages.value[i];
      if (row.role === CHAT_ROLE.TOOL && row.turn_num === turnNum) {
        row.toolStatus = 'done';
        break;
      }
    }
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
  chatMessages.value = chatMessages.value.map((m) => {
    if (m.role === CHAT_ROLE.TOOL && m.toolStatus === 'running') {
      changed = true;
      return { ...m, toolStatus: 'failed' as const };
    }
    return m;
  });
  if (!changed) chatMessages.value = [...chatMessages.value];
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
  const turnNum =
    chatMessages.value.reduce((max, m) => Math.max(max, m.turn_num), 0) + 1;

  // 携带本次待发送的图片（发送时取走并清空待发送列表）
  const imageBase64List = selectedImages.value.map((img) => img.base64);

  // 追加用户消息（本地即时显示）
  const userMsg: MessageItem = {
    session_id: sid,
    role: CHAT_ROLE.USER,
    content: text,
    images: imageBase64List,
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

  // 发送后清空待发送图片与输入区
  selectedImages.value = [];
  draft.value = '';

  isSending.value = true;

  /**
   * 流式 chunk 回调：复用共享的 `appendStreamChunk` 按语义类型动态管理消息分段
   * （text/tool_start/tool_end），与 HITL resume 路径共用同一套渲染逻辑。
   */
  const onStreamChunk = (content: string, type: AgentChunkType) => {
    appendStreamChunk(sid, content, type, turnNum);
  };

  try {
    const req = { text };
    if (imageBase64List.length > 0) req.image_base64_list = imageBase64List;
    activeAgentController = postAgentStream(
      sid,
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
        // 流式出错：进行中的工具调用未正常完成，标记为 failed（红 ✗）
        markRunningToolsFailed();
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
  // 工具栏
  switch (event) {
    case 'createSession':
      handleCreateSession();
      return;
    case 'uploadFile':
      return;
    case 'uploadImage':
      triggerImagePicker();
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

// 首屏加载当前会话的历史消息，获取合并后的列表渲染到 ChatBox
watch(
  sessionId,
  (sid) => {
    if (!sid) return;
    // 加载该会话已锁定的角色快照（无快照则用全局 profile 锁定）
    ensureSessionCharacter(sid);
    loadSessionHistory(sid);
    // 恢复三层持久化场景下可能仍待审批的 HITL 中断卡（切 session / 刷新 / 重开 / 服务重启）
    restorePendingHitl(sid);
  },
  { immediate: true },
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
