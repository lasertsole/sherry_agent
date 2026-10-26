<template>
  <div class="flex h-full min-h-0 flex-col">
    <div
      v-if="!sid"
      class="flex h-full flex-col items-center justify-center gap-2 px-4 text-center text-xs text-gray-500 dark:text-gray-400">
      <i class="pi pi-comments text-lg"></i>
      <span>{{ t('chat.selection.sideChatMissing') }}</span>
    </div>
    <template v-else>
      <div
        v-if="props.payload?.inherited"
        class="mx-2 mt-2 shrink-0 rounded-md border border-dashed border-gray-200 px-2.5 py-1.5 text-center text-[11px] text-gray-500 dark:border-gray-700 dark:text-gray-400"
        data-test="side-chat-inherited-hint">
        {{ t('chat.selection.sideChatInherited') }}
      </div>
      <ChatBox
        :messages="chatMessages"
        :user-avatar="characterInfo.userAvatar"
        :ai-avatar="characterInfo.aiAvatar"
        :user-name="characterInfo.userName"
        :ai-name="characterInfo.aiName"
        :selection-enabled="!hitlRequest"
        :allow-side-chat="false"
        @selection-add="onSelectionAdd" />
      <!-- The approval card and the composer share the same slot: a pending
           approval owns the input, exactly like the main chat. -->
      <div class="flex h-40 shrink-0 flex-col border-t border-solid border-gray-light dark:border-gray-dark">
        <div
          v-if="hitlRequest"
          class="flex flex-1 min-h-0 flex-col gap-2 overflow-y-auto p-2">
          <!-- question tool: options + a trailing custom answer -->
          <template v-if="isQuestion">
            <div class="text-sm font-semibold">{{ questionHeader || t('hitl.title') }}</div>
            <div
              v-if="questionText"
              class="text-sm whitespace-pre-wrap text-gray-700 dark:text-gray-200">
              {{ questionText }}
            </div>
            <div class="flex flex-col gap-1.5">
              <button
                v-for="(option, idx) in questionOptions"
                :key="idx"
                type="button"
                class="cursor-pointer rounded-lg border border-solid px-3 py-1.5 text-left transition-colors"
                :class="
                  isMultipleQuestion && selectedOptions.includes(option.label)
                    ? 'border-theme-main bg-theme-main/10'
                    : 'border-gray-light hover:bg-gray-50 dark:border-gray-dark dark:hover:bg-[#1a1d21]'
                "
                @click="selectOption(option.label)">
                <span class="text-sm font-medium">{{ option.label }}</span>
                <span
                  v-if="option.description"
                  class="ml-2 text-xs text-gray-400">
                  {{ option.description }}
                </span>
              </button>
              <div class="flex items-center gap-2">
                <input
                  v-model="customAnswer"
                  type="text"
                  class="min-w-0 flex-1 rounded-lg border border-solid border-gray-light bg-transparent px-3 py-1.5 text-sm outline-none focus:border-theme-main dark:border-gray-dark"
                  :placeholder="t('hitl.customPlaceholder')"
                  @keydown.enter.prevent="submitCustomAnswer" />
                <Button
                  :label="t('hitl.submit')"
                  size="small"
                  :disabled="!customAnswer.trim() && selectedOptions.length === 0"
                  @click="submitCustomAnswer" />
                <Button
                  :label="t('hitl.reject')"
                  size="small"
                  severity="danger"
                  variant="text"
                  @click="handleHitlDecision('reject')" />
              </div>
            </div>
          </template>
          <!-- exit_plan_mode: approve or send back with feedback -->
          <template v-else-if="isPlan">
            <div class="text-sm font-semibold">{{ t('hitl.planTitle') }}</div>
            <pre
              class="m-0 min-h-0 flex-1 overflow-auto whitespace-pre-wrap rounded-lg bg-gray-50 p-2 text-xs dark:bg-gray-800"
              >{{ planText }}</pre>
            <div class="flex items-center gap-2">
              <input
                v-model="planFeedback"
                type="text"
                class="min-w-0 flex-1 rounded-lg border border-solid border-gray-light bg-transparent px-3 py-1.5 text-sm outline-none focus:border-theme-main dark:border-gray-dark"
                :placeholder="t('hitl.planFeedbackPlaceholder')"
                @keydown.enter.prevent="revisePlan" />
              <Button
                :label="t('hitl.planRevise')"
                size="small"
                severity="warn"
                variant="text"
                :disabled="!planFeedback.trim()"
                @click="revisePlan" />
              <Button
                :label="t('hitl.planApprove')"
                icon="pi pi-check"
                size="small"
                @click="handleHitlDecision('approve')" />
            </div>
          </template>
          <!-- any other tool: generic approval -->
          <template v-else>
            <div class="text-sm font-semibold">{{ t('hitl.title') }}</div>
            <div class="text-sm text-gray-500">
              {{ t('hitl.tool') }}: <span class="font-bold">{{ hitlRequest.tool_name }}</span>
            </div>
            <div
              v-if="hitlRequest.description"
              class="max-h-24 overflow-auto whitespace-pre-wrap text-sm">
              {{ hitlRequest.description }}
            </div>
            <div class="mt-auto flex justify-end gap-2">
              <Button
                :label="t('hitl.reject')"
                icon="pi pi-times"
                severity="danger"
                size="small"
                @click="handleHitlDecision('reject')" />
              <Button
                :label="t('hitl.yolo')"
                icon="pi pi-bolt"
                severity="warn"
                size="small"
                :title="t('hitl.yoloTooltip')"
                @click="handleHitlDecision('yolo')" />
              <Button
                :label="t('hitl.approve')"
                icon="pi pi-check"
                size="small"
                @click="handleHitlDecision('approve')" />
            </div>
          </template>
        </div>
        <ChatInputBox
          v-else
          ref="chatInputBoxRef"
          v-model:draft="draft"
          :sending="isSending"
          @send="handleSend"
          @stop="handleStop" />
      </div>
    </template>
  </div>
</template>

<script lang="ts" setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import ChatBox from './ChatBox.vue';
import { ChatInputBox } from '#components';
import type { MessageItem } from '../type';
import type { ChatController } from '~/composables/messages';
import type { SelectionPayload } from '~/composables/use-chat-selection';
import { CHAT_ROLE } from '@/types/chat-role';

/**
 * A helper session opened from a quoted selection, hosted by the right sidebar.
 *
 * The panel is a compact chat page: the same lifecycle / draft / chunk / HITL
 * slices the main page uses, over the child session's own persistent socket,
 * history and approvals. It is not a subagent — the child is an ordinary
 * session the user talks to directly.
 */
interface PanelPayload {
  sessionId?: string;
  parentSessionId?: string;
  inherited?: boolean;
  quote?: string;
  source?: string;
  /** False when a reopened side chat must not re-insert its original quote. */
  prefillQuote?: boolean;
}

const props = defineProps<{ payload?: PanelPayload }>();

const { t } = useI18n();
const sid = String(props.payload?.sessionId || '');

const chatMessages = ref<MessageItem[]>([]);
const draft = ref('');
const isSending = ref(false);
const activeAgentController = ref<ChatController | null>(null);
const streamingTurn = ref<number | null>(null);
const chatInputBoxRef = useTemplateRef<{
  addQuoteChip?: (text: string, source?: string) => void;
}>('chatInputBoxRef');

const sessionId = ref(sid);
const agentSocket = acquireAgentSocket(sid);
const { characterInfo, ensureSessionCharacter, loadSessionHistory } = useSessionLifecycle(chatMessages);
const drafts = useDraftPersistence(chatMessages);
const chunks = useStreamChunks(chatMessages, drafts.allocateTempId, drafts);

const hitl = useHitlApproval({
  chatMessages,
  sessionId,
  isSending,
  activeAgentController,
  socket: agentSocket,
  streamingTurn,
  loadSessionHistory,
  drafts,
  chunks
});
const { hitlRequest, handleHitlDecision, restorePendingHitl } = hitl;

const stream = useChatStream({
  chatMessages,
  sessionId,
  mySid: sid,
  draft,
  isSending,
  activeAgentController,
  socket: agentSocket,
  streamingTurn,
  t,
  getPendingMedia: () => ({ images: [], audios: [], videos: [] }),
  clearMediaSelection: () => {},
  loadSessionHistory,
  drafts,
  chunks,
  hitl
});
const { handleSend, handleStop, handleSocketChunk, handleTurnStarted, handleQueued, handleSocketDone } = stream;

/**
 * The localized "from X" tooltip for a quote chip.
 * @param role The quoted message's role.
 * @returns The localized source label.
 */
function selectionRoleLabel(role?: string): string {
  if (role === CHAT_ROLE.USER) return t('chat.selection.sourceUser');
  if (role === CHAT_ROLE.AI) return t('chat.selection.sourceAi');
  return t('chat.selection.sourceTool');
}

/**
 * The "添加到当前会话" (add to this conversation) item inside the side panel re-quotes into this panel's composer.
 * @param payload
 */
function onSelectionAdd(payload: SelectionPayload): void {
  if (!payload?.text) return;
  chatInputBoxRef.value?.addQuoteChip?.(payload.text, selectionRoleLabel(payload.sourceRole));
}

// ── HITL view state (the question tool and the plan approval) ──
const toolArgs = computed<Record<string, unknown>>(() => hitlRequest.value?.tool_args ?? {});
const isQuestion = computed(() => hitlRequest.value?.tool_name === 'question');
const isPlan = computed(() => hitlRequest.value?.tool_name === 'exit_plan_mode');
const questionText = computed(() => {
  const question = toolArgs.value['question'];
  return typeof question === 'string' ? question : (hitlRequest.value?.description ?? '');
});
const questionHeader = computed(() => {
  const header = toolArgs.value['header'];
  return typeof header === 'string' ? header : '';
});
const questionOptions = computed(() => {
  const raw = toolArgs.value['options'];
  if (!Array.isArray(raw)) return [] as Array<{ label: string; description?: string }>;
  return raw
    .filter((option): option is Record<string, unknown> => typeof option === 'object' && option !== null)
    .map(option => ({
      label: String(option['label'] ?? ''),
      description: typeof option['description'] === 'string' ? option['description'] : undefined
    }))
    .filter(option => option.label.length > 0);
});
const isMultipleQuestion = computed(() => toolArgs.value['multiple'] === true);
const customAnswer = ref('');
const selectedOptions = ref<string[]>([]);
const planText = computed(() => {
  const plan = toolArgs.value['plan'];
  return typeof plan === 'string' ? plan : (hitlRequest.value?.description ?? '');
});
const planFeedback = ref('');

/**
 * Send the chosen answer through the HITL resume.
 * @param answer
 */
function submitAnswer(answer: string): void {
  customAnswer.value = '';
  selectedOptions.value = [];
  handleHitlDecision('approve', answer);
}

/**
 * Single-select sends immediately; multi-select toggles the option.
 * @param label
 */
function selectOption(label: string): void {
  if (!isMultipleQuestion.value) {
    submitAnswer(label);
    return;
  }
  const index = selectedOptions.value.indexOf(label);
  if (index >= 0) selectedOptions.value.splice(index, 1);
  else selectedOptions.value.push(label);
}

/** Submit the custom answer plus any selected options. */
function submitCustomAnswer(): void {
  const parts = isMultipleQuestion.value ? [...selectedOptions.value] : [];
  const text = customAnswer.value.trim();
  if (text) parts.push(text);
  if (parts.length) submitAnswer(parts.join(', '));
}

/** Send the plan back with feedback (reject + message = revise). */
function revisePlan(): void {
  const message = planFeedback.value.trim();
  if (!message) return;
  planFeedback.value = '';
  handleHitlDecision('reject', message);
}

// A new request starts from a clean question/plan form.
watch(hitlRequest, () => {
  customAnswer.value = '';
  selectedOptions.value = [];
  planFeedback.value = '';
});

onMounted(async () => {
  if (!sid) return;
  agentSocket.setHandlers({
    onChunk: handleSocketChunk,
    onHitl: hitl.handleHitlRequest,
    onTurnStarted: handleTurnStarted,
    onQueued: handleQueued,
    onQueuedCancelled: stream.handleQueuedCancelled,
    onQueuedUpdated: stream.handleQueuedUpdated,
    onSendNowAck: stream.handleSendNowAck,
    onDone: handleSocketDone
  });
  void ensureSessionCharacter(sid);
  await nextTick();
  if (props.payload?.quote && props.payload.prefillQuote !== false) {
    chatInputBoxRef.value?.addQuoteChip?.(props.payload.quote, props.payload.source);
  }
  void stream.syncHistory(sid);
  void restorePendingHitl(sid);
});

onBeforeUnmount(() => {
  drafts.dispose();
});
</script>

<i18n lang="json">
{
  "zh": {
    "hitl": {
      "approve": "批准",
      "reject": "拒绝",
      "title": "操作需要审批",
      "tool": "工具",
      "yolo": "同意所有操作",
      "yoloTooltip": "同意本次及本会话后续所有操作，不再弹出审批",
      "customPlaceholder": "输入自定义回答…",
      "submit": "提交",
      "planTitle": "计划待审批",
      "planApprove": "批准计划",
      "planRevise": "退回修改",
      "planFeedbackPlaceholder": "指出需要修改的地方…"
    }
  },
  "en": {
    "hitl": {
      "approve": "Approve",
      "reject": "Reject",
      "title": "Action Requires Approval",
      "tool": "Tool",
      "yolo": "Approve All (YOLO)",
      "yoloTooltip": "Approve this and all future actions in this session — no more approval prompts",
      "customPlaceholder": "Type a custom answer…",
      "submit": "Submit",
      "planTitle": "Plan Awaiting Approval",
      "planApprove": "Approve Plan",
      "planRevise": "Request Changes",
      "planFeedbackPlaceholder": "What should change? …"
    }
  },
  "ja": {
    "hitl": {
      "approve": "承認",
      "reject": "拒否",
      "title": "操作の承認が必要です",
      "tool": "ツール",
      "yolo": "すべての操作を承認",
      "yoloTooltip": "今回とこのセッションの以後の操作をすべて承認し、確認ダイアログは表示されません",
      "customPlaceholder": "カスタム回答を入力…",
      "submit": "送信",
      "planTitle": "計画の承認待ち",
      "planApprove": "計画を承認",
      "planRevise": "修正を依頼",
      "planFeedbackPlaceholder": "修正すべき点を入力…"
    }
  },
  "ko": {
    "hitl": {
      "approve": "승인",
      "reject": "거부",
      "title": "작업 승인이 필요합니다",
      "tool": "도구",
      "yolo": "모든 작업 승인",
      "yoloTooltip": "이번 작업과 이 세션의 이후 모든 작업을 승인하며 확인 창이 다시 표시되지 않습니다",
      "customPlaceholder": "직접 답변 입력…",
      "submit": "제출",
      "planTitle": "계획 승인 대기",
      "planApprove": "계획 승인",
      "planRevise": "수정 요청",
      "planFeedbackPlaceholder": "수정할 점을 입력하세요…"
    }
  }
}
</i18n>
