<template>
  <div
    class="flex h-full min-h-0 flex-col"
    data-test="context-viewer">
    <!-- Header: what the tab shows, the window it is measured against, and how
         fresh the numbers are. The view refreshes itself while it is open. -->
    <div
      class="flex shrink-0 flex-wrap items-center gap-2 border-b border-solid border-gray-100 px-3 py-2 text-xs dark:border-gray-800">
      <i class="pi pi-database text-theme-main"></i>
      <span class="text-gray-600 dark:text-gray-300">{{ t('contextViewer.title') }}</span>
      <span
        v-if="data"
        class="font-mono text-gray-400 dark:text-gray-500"
        data-test="context-window">
        {{ t('contextViewer.windowLabel', { window: formattedWindow }) }}
      </span>
      <span
        class="ml-auto flex items-center gap-1.5 text-[11px] text-gray-400 dark:text-gray-500"
        data-test="context-updated">
        <span
          class="inline-block h-1.5 w-1.5 rounded-full"
          :class="badgeClass" />
        {{ statusText }}
      </span>
      <Button
        icon="pi pi-refresh"
        size="small"
        text
        severity="secondary"
        :title="t('contextViewer.refresh')"
        :aria-label="t('contextViewer.refresh')"
        data-test="context-refresh"
        @click="load" />
    </div>

    <div class="min-h-0 flex-1 overflow-auto px-3 py-2">
      <div
        v-if="loading && !data"
        class="flex items-center justify-center py-8">
        <ProgressSpinner style="width: 1.5rem; height: 1.5rem" />
      </div>

      <p
        v-else-if="!data"
        class="m-0 text-xs text-gray-400 dark:text-gray-500"
        data-test="context-empty">
        {{ t('contextViewer.empty') }}
      </p>

      <template v-else>
        <p
          v-if="data.state_error"
          class="m-0 mb-2 rounded bg-amber-50 px-2 py-1 text-[11px] text-amber-700 dark:bg-amber-900/30 dark:text-amber-300"
          data-test="context-state-error">
          {{ t('contextViewer.stateError', { error: data.state_error }) }}
        </p>

        <!-- One section per part of the context: 系统提示词 / 工具 / 消息队列. -->
        <section
          v-for="section in sections"
          :key="section.key"
          class="mb-2 rounded border border-solid border-gray-100 dark:border-gray-800">
          <button
            type="button"
            class="flex w-full cursor-pointer items-center gap-1.5 px-2 py-1.5 text-left text-xs text-gray-600 hover:bg-gray-50 dark:text-gray-300 dark:hover:bg-gray-800/60"
            :aria-expanded="open[section.key]"
            :data-test="`context-section-${section.key}`"
            @click="toggleSection(section.key)">
            <i
              class="pi text-[10px] transition-transform duration-200"
              :class="open[section.key] ? 'pi-chevron-down' : 'pi-chevron-right'" />
            <i :class="[section.icon, 'text-theme-main']" />
            <span>{{ t(section.label) }}</span>
            <span class="text-gray-400 dark:text-gray-500">
              · {{ t('contextViewer.items', { n: section.count }) }} · ≈{{ formatTokens(section.tokens) }}
              {{ t('contextViewer.tokens') }}
            </span>
            <span
              v-if="section.note"
              class="ml-auto truncate text-[11px] text-gray-400 dark:text-gray-500">
              {{ t(section.note) }}
            </span>
          </button>

          <div
            v-if="open[section.key]"
            class="border-t border-solid border-gray-100 px-2 py-1.5 dark:border-gray-800"
            :data-test="`context-body-${section.key}`">
            <!-- System prompt: the assembled text, verbatim. -->
            <pre
              v-if="section.key === 'system'"
              class="m-0 max-h-[45vh] overflow-auto whitespace-pre-wrap break-words font-mono text-[11px] leading-relaxed text-gray-600 dark:text-gray-300"
              data-test="context-system-text"
              >{{ data.system_prompt || t('contextViewer.emptyPart') }}</pre>

            <!-- Tools: one collapsible row per definition (name + description + schema). -->
            <div
              v-else-if="section.key === 'tools'"
              class="flex flex-col gap-1">
              <div
                v-for="tool in data.tools"
                :key="tool.name"
                class="rounded bg-gray-50/70 px-2 py-1 dark:bg-gray-800/40">
                <button
                  type="button"
                  class="flex w-full cursor-pointer items-center gap-1.5 text-left font-mono text-[11px] text-theme-main"
                  :aria-expanded="openTools.has(tool.name)"
                  :data-test="`context-tool-${tool.name}`"
                  @click="toggleTool(tool.name)">
                  <i
                    class="pi text-[10px] transition-transform duration-200"
                    :class="openTools.has(tool.name) ? 'pi-chevron-down' : 'pi-chevron-right'" />
                  <span class="truncate">{{ tool.name }}</span>
                  <span class="ml-auto shrink-0 text-[10px] text-gray-400 dark:text-gray-500">
                    {{ tool.parameters ? t('contextViewer.hasSchema') : t('contextViewer.noSchema') }}
                  </span>
                </button>
                <p class="m-0 mt-0.5 pl-4 text-[11px] text-gray-500 dark:text-gray-400">
                  {{ tool.description || t('contextViewer.emptyPart') }}
                </p>
                <pre
                  v-if="openTools.has(tool.name) && tool.parameters"
                  class="m-0 mt-1 max-h-[30vh] overflow-auto whitespace-pre-wrap break-words rounded bg-white/70 px-2 py-1 font-mono text-[10px] text-gray-500 dark:bg-gray-900/40 dark:text-gray-400"
                  >{{ JSON.stringify(tool.parameters, null, 2) }}</pre>
              </div>
            </div>

            <!-- Messages: the live transcript, one row per message. -->
            <div
              v-else
              class="flex flex-col gap-1.5">
              <div
                v-for="(message, index) in data.messages"
                :key="index"
                class="rounded bg-gray-50/70 px-2 py-1 dark:bg-gray-800/40"
                :data-test="`context-message-${index}`">
                <div class="flex items-center gap-1.5 text-[10px] text-gray-400 dark:text-gray-500">
                  <span
                    class="rounded px-1 font-mono"
                    :class="roleClass(message.role)"
                    >{{ message.role }}</span
                  >
                  <span class="font-mono">#{{ index }}</span>
                  <span
                    v-if="message.origin"
                    class="text-amber-600 dark:text-amber-400"
                    >origin={{ message.origin }}</span
                  >
                  <span
                    v-if="message.truncated"
                    class="text-amber-600 dark:text-amber-400"
                    >truncated</span
                  >
                </div>
                <!-- The model's chain-of-thought: a collapsed block per row, ABOVE
                     the reply it produced — the order the chat shows it in (think,
                     then answer), and the order it was actually generated in. A
                     thinking-only row would otherwise read as empty. -->
                <div
                  v-if="message.reasoning"
                  class="mt-0.5">
                  <button
                    type="button"
                    class="flex cursor-pointer items-center gap-1 text-[10px] text-gray-400 hover:text-[#2563EB] dark:text-gray-500 dark:hover:text-[#60A5FA]"
                    :aria-expanded="openReasoning.has(index)"
                    :data-test="`context-reasoning-${index}`"
                    @click="toggleReasoning(index)">
                    <i
                      class="pi text-[9px] transition-transform duration-200"
                      :class="openReasoning.has(index) ? 'pi-chevron-down' : 'pi-chevron-right'" />
                    {{ t('contextViewer.reasoning') }}
                  </button>
                  <pre
                    v-if="openReasoning.has(index)"
                    class="m-0 mt-0.5 max-h-[30vh] overflow-auto whitespace-pre-wrap break-words rounded bg-white/70 px-2 py-1 font-mono text-[10px] text-gray-500 dark:bg-gray-900/40 dark:text-gray-400"
                    data-test="context-reasoning-body"
                    >{{ message.reasoning }}</pre>
                </div>
                <pre
                  v-if="message.content"
                  class="m-0 mt-0.5 whitespace-pre-wrap break-words font-mono text-[11px] text-gray-600 dark:text-gray-300"
                  data-test="context-content"
                  >{{ message.content }}</pre>
                <div
                  v-for="call in message.tool_calls ?? []"
                  :key="call.name"
                  class="mt-0.5 font-mono text-[10px] text-gray-500 dark:text-gray-400">
                  → {{ call.name }}({{ call.args }})
                </div>
              </div>
              <p
                v-if="data.messages.length === 0"
                class="m-0 text-[11px] text-gray-400 dark:text-gray-500"
                data-test="context-messages-empty">
                {{ t('contextViewer.emptyPart') }}
              </p>
            </div>
          </div>
        </section>
      </template>
    </div>
  </div>
</template>

<script lang="ts" setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import { useRoute } from 'vue-router';
import type { ContextInspect } from '~/composables/bridge/session';
// Stable module specifier so tests can vi.mock the bridge (the unimport
// injection is compile-time and leaves bare symbols unmockable).
/* eslint-disable @typescript-eslint/no-restricted-imports */
import { fetchContextInspect } from '~/composables/bridge/session';
/* eslint-enable @typescript-eslint/no-restricted-imports */
import { logUtil } from '~/utils/log';

const { t } = useI18n({ useScope: 'local' });

/** How often the open view re-reads the live context. */
const REFRESH_MS = 3000;

const route = useRoute();
const sessionId = computed(() => (typeof route.params.sid === 'string' ? route.params.sid : ''));

const data = ref<ContextInspect | null>(null);
const loading = ref(false);
/** When the last successful read landed, and a 1s tick that keeps the label honest. */
const lastLoadedAt = ref(0);
const now = ref(Date.now());
/** Sections start open: the point of the tab is to read them. */
const open = ref<Record<'system' | 'tools' | 'messages', boolean>>({
  system: true,
  tools: false,
  messages: true
});
/** Tool schemas are opened one at a time (they are long). */
const openTools = ref<Set<string>>(new Set());
/** Reasoning blocks, by message index (a thought is long; opened on demand). */
const openReasoning = ref<Set<number>>(new Set());
let timer: ReturnType<typeof setInterval> | null = null;

const toggleSection = (key: 'system' | 'tools' | 'messages'): void => {
  open.value = { ...open.value, [key]: !open.value[key] };
};
const toggleReasoning = (index: number): void => {
  const next = new Set(openReasoning.value);
  if (next.has(index)) next.delete(index);
  else next.add(index);
  openReasoning.value = next;
};
const toggleTool = (name: string): void => {
  const next = new Set(openTools.value);
  if (next.has(name)) next.delete(name);
  else next.add(name);
  openTools.value = next;
};

/** Per-part figures the section headers show (count, tokens, note). */
const sections = computed(() => {
  const view = data.value;
  return [
    {
      key: 'system' as const,
      icon: 'pi pi-align-left',
      label: 'contextViewer.system',
      count: view?.system_prompt ? 1 : 0,
      tokens: view?.system_tokens ?? 0,
      note: ''
    },
    {
      key: 'tools' as const,
      icon: 'pi pi-wrench',
      label: 'contextViewer.tools',
      count: view?.tools.length ?? 0,
      tokens: view?.tool_tokens ?? 0,
      note: view?.tool_selection ? 'contextViewer.selectionOn' : 'contextViewer.selectionOff'
    },
    {
      key: 'messages' as const,
      icon: 'pi pi-comments',
      label: 'contextViewer.messages',
      count: view?.messages.length ?? 0,
      tokens: view?.message_tokens ?? 0,
      note: view?.truncated ? 'contextViewer.clipped' : ''
    }
  ];
});

/**
 * Read one snapshot of the session's context. Failures keep the previous
 * snapshot (a live view must not blank out on a hiccup) and only log.
 */
const load = async (): Promise<void> => {
  if (!sessionId.value) return;
  loading.value = true;
  try {
    data.value = await fetchContextInspect(sessionId.value);
    lastLoadedAt.value = Date.now();
  } catch (e) {
    logUtil.e('[ContextViewerPanel] Failed to read the context:', e);
  } finally {
    loading.value = false;
  }
};

let clock: ReturnType<typeof setInterval> | null = null;

const statusText = computed(() => {
  if (loading.value && !data.value) return t('contextViewer.loading');
  if (!lastLoadedAt.value) return t('contextViewer.empty');
  const age = Math.max(0, Math.round((now.value - lastLoadedAt.value) / 1000));
  return age <= 1 ? t('contextViewer.live') : t('contextViewer.stale', { seconds: age });
});
const badgeClass = computed(() => (!data.value || loading.value ? 'bg-gray-300 dark:bg-gray-600' : 'bg-emerald-500'));

/** Window in the locale's unit — the usage ring's own scale (万 / k / 万 / 만). */
const formattedWindow = computed<string>(() => formatTokens(data.value?.window ?? 0));

/**
 * Token figure in the current locale's unit (the usage ring's scale, so the two
 * surfaces read the same).
 * @param tokens
 */
function formatTokens(tokens: number): string {
  const scale = Number(t('contextViewer.tokenUnitScale')) || 1000;
  const unit = t('contextViewer.tokenUnit');
  const value = tokens / scale;
  const rounded = value >= 100 || Number.isInteger(value) ? Math.round(value) : Number(value.toFixed(1));
  return `${rounded}${unit}`;
}

/**
 * Role tint for a transcript chip: human / ai / tool each get their own.
 * @param role
 */
const roleClass = (role: string): string => {
  if (role === 'human') return 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300';
  if (role === 'ai') return 'bg-sky-100 text-sky-700 dark:bg-sky-900/40 dark:text-sky-300';
  if (role === 'tool') return 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300';
  return 'bg-gray-100 text-gray-500 dark:bg-gray-800 dark:text-gray-400';
};

onMounted(() => {
  void load();
  timer = setInterval(() => void load(), REFRESH_MS);
  clock = setInterval(() => (now.value = Date.now()), 1000);
});
onBeforeUnmount(() => {
  if (timer) clearInterval(timer);
  if (clock) clearInterval(clock);
  timer = null;
  clock = null;
});

// A session switch must not keep showing the previous session's transcript.
watch(sessionId, () => {
  data.value = null;
  void load();
});
</script>

<i18n lang="json">
{
  "zh": {
    "contextViewer": {
      "title": "Agent 上下文",
      "windowLabel": "窗口 {window}",
      "refresh": "立即刷新",
      "empty": "暂无数据",
      "loading": "读取中…",
      "live": "实时",
      "stale": "{seconds}s 前更新",
      "system": "系统提示词",
      "tools": "工具",
      "messages": "消息队列",
      "tokens": "tokens",
      "tokenUnit": "万",
      "tokenUnitScale": "10000",
      "items": "{n} 项",
      "selectionOn": "已按会话选择裁剪工具",
      "selectionOff": "全部工具",
      "clipped": "含截断内容",
      "hasSchema": "有参数",
      "noSchema": "无参数",
      "emptyPart": "（空）",
      "reasoning": "思考过程",
      "stateError": "读取消息列表失败：{error}"
    }
  },
  "en": {
    "contextViewer": {
      "title": "Agent context",
      "windowLabel": "window {window}",
      "refresh": "Refresh now",
      "empty": "No data yet",
      "loading": "Loading…",
      "live": "live",
      "stale": "updated {seconds}s ago",
      "system": "System prompt",
      "tools": "Tools",
      "messages": "Messages",
      "tokens": "tokens",
      "tokenUnit": "k",
      "tokenUnitScale": "1000",
      "items": "{n} items",
      "selectionOn": "narrowed by this session",
      "selectionOff": "all tools",
      "clipped": "contains clipped rows",
      "hasSchema": "with args",
      "noSchema": "no args",
      "emptyPart": "(empty)",
      "reasoning": "Thinking",
      "stateError": "Reading the message list failed: {error}"
    }
  },
  "ja": {
    "contextViewer": {
      "title": "エージェント コンテキスト",
      "windowLabel": "ウィンドウ {window}",
      "refresh": "今すぐ更新",
      "empty": "データなし",
      "loading": "読み込み中…",
      "live": "ライブ",
      "stale": "{seconds} 秒前に更新",
      "system": "システム プロンプト",
      "tools": "ツール",
      "messages": "メッセージ",
      "tokens": "トークン",
      "tokenUnit": "万",
      "tokenUnitScale": "10000",
      "items": "{n} 件",
      "selectionOn": "セッション選択で絞り込み",
      "selectionOff": "すべてのツール",
      "clipped": "切り詰めた行あり",
      "hasSchema": "引数あり",
      "noSchema": "引数なし",
      "emptyPart": "（空）",
      "reasoning": "思考プロセス",
      "stateError": "メッセージ一覧の読み取りに失敗: {error}"
    }
  },
  "ko": {
    "contextViewer": {
      "title": "에이전트 컨텍스트",
      "windowLabel": "윈도 {window}",
      "refresh": "지금 새로고침",
      "empty": "데이터 없음",
      "loading": "불러오는 중…",
      "live": "실시간",
      "stale": "{seconds}초 전 갱신",
      "system": "시스템 프롬프트",
      "tools": "도구",
      "messages": "메시지",
      "tokens": "토큰",
      "tokenUnit": "만",
      "tokenUnitScale": "10000",
      "items": "{n}개",
      "selectionOn": "세션 선택으로 좁힘",
      "selectionOff": "모든 도구",
      "clipped": "잘린 행 포함",
      "hasSchema": "인자 있음",
      "noSchema": "인자 없음",
      "emptyPart": "(비어 있음)",
      "reasoning": "사고 과정",
      "stateError": "메시지 목록 읽기 실패: {error}"
    }
  }
}
</i18n>
