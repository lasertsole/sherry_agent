<template>
  <!-- Floating turn navigator: one mark per recent user message, pinned to the
       left edge of the history area. Translucent + frosted so it reads as an
       overlay rather than part of the list, and it only carries the last
       TURN_SCRUBBER_LIMIT turns. -->
  <nav
    v-if="marks.length > 1"
    class="absolute left-0 top-1/2 z-10 flex max-h-[72%] -translate-y-1/2 flex-col items-center gap-1.5 overflow-y-auto rounded-full border border-white/25 bg-white/25 px-1 py-2 shadow-sm backdrop-blur-sm dark:border-white/5 dark:bg-black/20"
    :aria-label="t('chatScrubber.label')">
    <!-- Each step is a 10px-tall hit target around a thin bar: the bars stay marks,
         but consecutive turns are a comfortable distance apart and clickable. -->
    <button
      v-for="(mark, index) in marks"
      :key="mark.rowIndex"
      type="button"
      class="group flex h-2.5 w-5 shrink-0 cursor-pointer items-center justify-center"
      :title="mark.preview || t('chatScrubber.jump', { turn: mark.turn })"
      :aria-label="t('chatScrubber.jump', { turn: mark.turn })"
      :aria-current="index === activeIndex ? 'true' : undefined"
      @click="emit('jump', mark.rowIndex)">
      <span
        class="rounded-full transition-all"
        :class="
          index === activeIndex
            ? 'h-1 w-4 bg-theme-main'
            : 'h-0.5 w-3.5 bg-gray-400/70 group-hover:w-4 group-hover:bg-gray-500 dark:bg-gray-500/70 dark:group-hover:bg-gray-300'
        "></span>
    </button>
  </nav>
</template>

<script setup lang="ts">
import { useI18n } from 'vue-i18n';
import type { ChatTurnMark } from '~/composables/use-chat-virtual-list';

const { t } = useI18n({ useScope: 'local' });

defineProps<{
  /** Marks in render order (oldest → newest), one per recent user message. */
  marks: ChatTurnMark[];
  /** Index (into `marks`) of the turn being read, or null when unknown. */
  activeIndex: number | null;
}>();

const emit = defineEmits<{ jump: [rowIndex: number] }>();
</script>

<i18n lang="json">
{
  "zh": {
    "chatScrubber": {
      "label": "历史消息穿梭器",
      "jump": "跳到第 {turn} 轮"
    }
  },
  "en": {
    "chatScrubber": {
      "label": "Turn navigator",
      "jump": "Jump to turn {turn}"
    }
  },
  "ja": {
    "chatScrubber": {
      "label": "ターンナビゲーター",
      "jump": "第 {turn} ターンへ移動"
    }
  },
  "ko": {
    "chatScrubber": {
      "label": "턴 내비게이터",
      "jump": "{turn}번째 턴으로 이동"
    }
  }
}
</i18n>
