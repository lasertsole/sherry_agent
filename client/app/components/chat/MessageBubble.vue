<template>
  <div
    class="message-bubble"
    :class="bubbleClass"
    data-test="message-bubble">
    <!-- Speaker label (meetings use it, chat does not) -->
    <div
      v-if="speakerName"
      class="flex items-center gap-2 mb-1"
      data-test="message-bubble-speaker">
      <span class="font-medium text-sm">{{ speakerName }}</span>
      <span
        v-if="roleBadge"
        class="px-1.5 py-0.5 rounded text-xs bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300"
        data-test="message-bubble-role"
        >{{ roleBadge }}</span
      >
    </div>
    <!-- Rendered markdown HTML; v-safe-html is globally registered -->
    <div
      v-safe-html="content"
      class="text-sm prose dark:prose-invert max-w-none"
      data-test="message-bubble-content" />
    <!-- Collapsible thinking -->
    <details
      v-if="thinking"
      class="mt-2"
      data-test="message-bubble-thinking">
      <summary class="text-xs text-gray-400 cursor-pointer">{{ thinkingLabel }}</summary>
      <div
        v-safe-html="thinking"
        class="text-xs text-gray-500 mt-1 prose dark:prose-invert" />
    </details>
    <!-- Parent-supplied actions (copy/fork/edit in chat, none in meetings) -->
    <slot name="actions" />
  </div>
</template>

<script setup lang="ts">
import { useI18n } from 'vue-i18n';

interface Props {
  /** Pre-rendered markdown HTML */
  content: string;
  /** Speaker display name (meeting messages) */
  speakerName?: string;
  /** Speaker role classification */
  speakerKind?: 'agent' | 'host' | 'user';
  /** Role label shown beside the speaker */
  roleBadge?: string;
  /** Role badge colour severity */
  roleSeverity?: string;
  /** Pre-rendered thinking markdown HTML */
  thinking?: string;
  /** Extra class on the bubble root */
  bubbleClass?: string;
}
defineProps<Props>();

const { t } = useI18n();
const thinkingLabel = t('chatBox.thinking');
</script>
