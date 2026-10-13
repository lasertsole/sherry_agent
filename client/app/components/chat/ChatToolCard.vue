<template>
  <!-- Tool call card -->
  <div
    class="flex flex-col gap-2 w-full px-3 py-2 rounded-lg border border-solid border-gray-200 dark:border-gray-600 bg-gray-50 dark:bg-gray-800/60 text-sm text-gray-600 dark:text-gray-300 overflow-x-auto"
    :class="highlighted ? 'ring-2 ring-theme-main/50 transition-shadow' : ''">
    <button
      type="button"
      class="flex items-center gap-2 w-full text-left cursor-pointer select-none"
      @click="$emit('toggle')">
      <span class="pi pi-hammer text-xs"></span>
      <span class="font-medium">{{ message.toolName }}</span>
      <span
        v-if="message.toolStatus === 'running'"
        class="pi pi-spin pi-spinner text-xs text-blue-500"></span>
      <span
        v-else-if="message.toolStatus === 'failed' || message.toolStatus === 'error'"
        class="pi pi-times text-xs text-red-500"></span>
      <span
        v-else
        class="pi pi-check text-xs text-green-500"></span>
      <!-- Duration: a live ticker while running (the same 1s cadence the
           toolbar's running-commands entry uses), the measured value once
           settled, nothing while unknown (a HITL denial or old data) -->
      <span
        v-if="durationText"
        class="text-xs tabular-nums text-gray-400 dark:text-gray-500"
        >{{ durationText }}</span
      >
      <span
        v-if="expandable"
        :class="[
          'pi pi-chevron-down text-xs ml-auto transition-transform duration-200',
          { 'rotate-180': expanded }
        ]"></span>
    </button>
    <!-- Expanded details: args + result (expanding to view live args/progress is also
         allowed while the tool is running) -->
    <div
      v-if="expanded"
      class="flex flex-col gap-2 border-t border-solid border-gray-200 dark:border-gray-600 pt-2">
      <div v-if="message.toolArgs && Object.keys(message.toolArgs).length">
        <div class="text-xs font-semibold mb-1">{{ argsLabel }}</div>
        <pre
          class="text-xs whitespace-pre-wrap break-words bg-white dark:bg-gray-900/60 rounded p-2 border border-solid border-gray-200 dark:border-gray-600"
          >{{ formatToolArgs(message.toolArgs) }}</pre>
      </div>
      <div v-if="message.toolResult">
        <div class="text-xs font-semibold mb-1">{{ resultLabel }}</div>
        <pre
          class="text-xs whitespace-pre-wrap break-words bg-white dark:bg-gray-900/60 rounded p-2 border border-solid border-gray-200 dark:border-gray-600"
          >{{ message.toolResult }}</pre>
      </div>
      <div
        v-if="message.toolStatus === 'running' && !message.toolResult"
        class="flex items-center gap-2 text-xs text-blue-500 dark:text-blue-400">
        <span class="pi pi-spin pi-spinner text-xs"></span>{{ runningLabel }}
      </div>
      <div
        v-else-if="!message.toolResult && !(message.toolArgs && Object.keys(message.toolArgs).length)"
        class="text-xs text-gray-400 dark:text-gray-500">
        {{ noOutputLabel }}
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onUnmounted, ref } from 'vue';
import type { MessageItem } from '~/pages/home/type';
import { formatElapsed, formatToolDuration } from '~/common/utils';

interface Props {
  /** The TOOL-role message rendered as a card */
  message: MessageItem;
  /** Whether the card body is expanded */
  expanded: boolean;
  /** Whether the card is expandable (has a tool name) */
  expandable: boolean;
  /** Whether the card was just jumped to (draws a transient ring) */
  highlighted?: boolean;
  /** Localized "Arguments" label */
  argsLabel: string;
  /** Localized "Result" label */
  resultLabel: string;
  /** Localized "Running…" label */
  runningLabel: string;
  /** Localized "No output" label */
  noOutputLabel: string;
}
const props = defineProps<Props>();
defineEmits<{ toggle: [] }>();

/**
 * The duration shown next to the status glyph.
 *
 * Running: the backend's measurement only arrives with the result frame, so the
 * card counts locally from the row's own start (its `timestamp`, the same
 * derivation the toolbar's running-commands entry uses). Settled: the measured
 * `toolDurationMs` — the authoritative value, present on history replay too.
 */
const nowMs = ref(Date.now());
let ticker: ReturnType<typeof setInterval> | null = null;
if (props.message.toolStatus === 'running') {
  ticker = setInterval(() => {
    nowMs.value = Date.now();
  }, 1000);
  onUnmounted(() => {
    if (ticker !== null) clearInterval(ticker);
  });
}

const durationText = computed(() => {
  if (props.message.toolStatus === 'running') {
    const startedAtMs = Date.parse(props.message.timestamp);
    if (Number.isNaN(startedAtMs)) return '';
    return formatElapsed(startedAtMs, nowMs.value) || '';
  }
  return formatToolDuration(props.message.toolDurationMs);
});

/**
 * Format the tool args object into readable JSON text
 * @param args
 */
function formatToolArgs(args: Record<string, unknown>): string {
  try {
    return JSON.stringify(args, null, 2);
  } catch {
    return String(args);
  }
}
</script>
