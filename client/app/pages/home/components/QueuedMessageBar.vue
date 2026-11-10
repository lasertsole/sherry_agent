<template>
  <div
    class="absolute top-0 left-0 right-0 z-20 bg-sky-600/90 text-white shadow-sm max-h-32 overflow-y-auto"
    role="status"
    aria-live="polite">
    <div
      v-for="badge in badges"
      :key="badge.msgId"
      class="flex items-center gap-2 py-1.5 px-3 text-xs font-medium border-b border-white/10 last:border-b-0">
      <i
        class="pi pi-clock"
        aria-hidden="true"></i>
      <span class="shrink-0">
        {{
          t('chatInput.queuedPosition', {
            position: badge.position,
            queueSize: badge.queueSize
          })
        }}
      </span>
      <input
        :value="badge.text"
        class="flex-1 min-w-0 rounded bg-white/10 px-1.5 py-0.5 outline-none focus:bg-white/20"
        :aria-label="t('chatInput.editQueued')"
        @input="emit('update-text', badge.msgId, ($event.target as HTMLInputElement).value)"
        @keydown.enter="($event.target as HTMLInputElement).blur()"
        @blur="emit('edit', badge.msgId, badge.text)" />
      <button
        class="shrink-0 rounded bg-white/20 px-2 py-0.5 font-medium transition-colors hover:bg-white/30"
        @click="emit('send-now', badge.msgId)">
        {{ t('chatInput.sendNow') }}
      </button>
      <button
        class="shrink-0 rounded bg-red-500/40 px-1.5 py-0.5 transition-colors hover:bg-red-500/60"
        :aria-label="t('chatInput.cancelQueued')"
        @click="emit('cancel', badge.msgId)">
        <i
          class="pi pi-times"
          style="font-size: 0.6rem"
          aria-hidden="true"></i>
      </button>
    </div>
  </div>
</template>

<script lang="ts" setup>
import { useI18n } from 'vue-i18n';
import type { QueueBadge } from '~/composables/use-chat-stream';

/**
 * The queued-messages banner above the composer: one row per message the
 * backend enqueued for this busy session (position, editable text, "send now",
 * cancel). The page owns the queue state and the socket calls.
 */
defineProps<{
  /** Queue rows, ordered by FIFO position. */
  badges: QueueBadge[];
}>();

const emit = defineEmits<{
  /** A row's text was edited (live, on input). */
  (e: 'update-text', msgId: string, text: string): void;
  /** The row's input blurred: persist the edited text. */
  (e: 'edit', msgId: string, text: string): void;
  /** Deliver this queued message next (interrupting the running turn). */
  (e: 'send-now', msgId: string): void;
  /** Void the queued message. */
  (e: 'cancel', msgId: string): void;
}>();

const { t } = useI18n();
</script>
