<template>
  <!-- Floating turn navigator: one mark per loaded user message, pinned to the
       left edge of the history area. No capsule of its own — the marks float over
       the list unobstructed. Only one page of marks is drawn at a time and the
       up/down arrows walk through the rest, so a long session stays reachable
       without a rail taller than the window. -->
  <nav
    v-if="marks.length > 1"
    class="scrubber-rail absolute left-0 top-1/2 z-10 flex max-h-[72%] -translate-y-1/2 flex-col items-center gap-1.5 overflow-y-auto px-1 py-2"
    :aria-label="t('chatScrubber.label')">
    <!-- Older page (the rail lists turns oldest → newest) -->
    <button
      v-if="pageCount > 1"
      type="button"
      class="flex h-3 w-5 shrink-0 cursor-pointer items-center justify-center text-gray-400 hover:text-gray-600 disabled:cursor-default disabled:opacity-30 dark:text-gray-500 dark:hover:text-gray-300"
      :disabled="atFirstPage"
      :title="t('chatScrubber.pageUp')"
      :aria-label="t('chatScrubber.pageUp')"
      @click="stepPage(-1)">
      <span class="pi pi-angle-up text-[10px]"></span>
    </button>
    <!-- Each step is a 10px-tall hit target around a thin bar: the bars stay marks,
         but consecutive turns are a comfortable distance apart and clickable. -->
    <button
      v-for="mark in visibleMarks"
      :key="mark.mark.rowIndex"
      type="button"
      class="group flex h-2.5 w-5 shrink-0 cursor-pointer items-center justify-center"
      :title="mark.mark.preview || t('chatScrubber.jump', { turn: mark.mark.turn })"
      :aria-label="t('chatScrubber.jump', { turn: mark.mark.turn })"
      :aria-current="mark.index === activeIndex ? 'true' : undefined"
      @click="emit('jump', mark.mark.rowIndex)">
      <span
        class="rounded-full transition-all"
        :class="
          mark.index === activeIndex
            ? 'h-1 w-4 bg-theme-main'
            : 'h-0.5 w-3.5 bg-gray-400/70 group-hover:w-4 group-hover:bg-gray-500 dark:bg-gray-500/70 dark:group-hover:bg-gray-300'
        "></span>
    </button>
    <!-- Newer page -->
    <button
      v-if="pageCount > 1"
      type="button"
      class="flex h-3 w-5 shrink-0 cursor-pointer items-center justify-center text-gray-400 hover:text-gray-600 disabled:cursor-default disabled:opacity-30 dark:text-gray-500 dark:hover:text-gray-300"
      :disabled="atLastPage"
      :title="t('chatScrubber.pageDown')"
      :aria-label="t('chatScrubber.pageDown')"
      @click="stepPage(1)">
      <span class="pi pi-angle-down text-[10px]"></span>
    </button>
  </nav>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
// The page size is auto-imported from the composable; only the type is imported.
import type { ChatTurnMark } from '~/composables/use-chat-virtual-list';

const { t } = useI18n({ useScope: 'local' });

const props = defineProps<{
  /** Marks in render order (oldest → newest), one per loaded user message. */
  marks: ChatTurnMark[];
  /** Index (into `marks`) of the turn being read, or null when unknown. */
  activeIndex: number | null;
}>();

const emit = defineEmits<{ jump: [rowIndex: number] }>();

/** First mark index drawn (the rail walks through `marks` in PAGE_SIZE steps). */
const pageStart = ref(0);

const pageCount = computed(() => Math.max(1, Math.ceil(props.marks.length / TURN_SCRUBBER_PAGE_SIZE)));
const atFirstPage = computed(() => pageStart.value <= 0);
const atLastPage = computed(() => pageStart.value + TURN_SCRUBBER_PAGE_SIZE >= props.marks.length);

/** The marks currently drawn, each paired with its index in the full list. */
const visibleMarks = computed(() =>
  props.marks
    .slice(pageStart.value, pageStart.value + TURN_SCRUBBER_PAGE_SIZE)
    .map((mark, offset) => ({ mark, index: pageStart.value + offset }))
);

/**
 * Move the drawn window one page.
 * @param direction -1 for the older page, 1 for the newer one.
 */
const stepPage = (direction: number) => {
  const next = pageStart.value + direction * TURN_SCRUBBER_PAGE_SIZE;
  pageStart.value = Math.min(Math.max(next, 0), (pageCount.value - 1) * TURN_SCRUBBER_PAGE_SIZE);
};

/**
 * Keep the turn being read on screen: scrolling the list can move the active mark
 * out of the drawn page, and the rail follows it.
 */
watch(
  () => props.activeIndex,
  active => {
    if (active == null) return;
    if (active < pageStart.value || active >= pageStart.value + TURN_SCRUBBER_PAGE_SIZE) {
      pageStart.value = Math.floor(active / TURN_SCRUBBER_PAGE_SIZE) * TURN_SCRUBBER_PAGE_SIZE;
    }
  }
);
</script>
<style scoped>
/* The rail scrolls when a full page of marks is taller than its cap, but its
   scrollbar would sit on top of the chat text just next to the first column of
   characters: hide it completely. The marks stay reachable — the wheel and the
   page arrows above/below the marks walk them. */
.scrubber-rail {
  scrollbar-width: none; /* Firefox */
  -ms-overflow-style: none; /* legacy Edge */
}

.scrubber-rail::-webkit-scrollbar {
  display: none; /* Chrome / Safari / WebKit */
  width: 0;
  height: 0;
}
</style>

<i18n lang="json">
{
  "zh": {
    "chatScrubber": {
      "label": "历史消息穿梭器",
      "jump": "跳到第 {turn} 轮",
      "pageUp": "更早的消息",
      "pageDown": "更新的消息"
    }
  },
  "en": {
    "chatScrubber": {
      "label": "Turn navigator",
      "jump": "Jump to turn {turn}",
      "pageUp": "Older messages",
      "pageDown": "Newer messages"
    }
  },
  "ja": {
    "chatScrubber": {
      "label": "ターンナビゲーター",
      "jump": "第 {turn} ターンへ移動",
      "pageUp": "古いメッセージ",
      "pageDown": "新しいメッセージ"
    }
  },
  "ko": {
    "chatScrubber": {
      "label": "턴 내비게이터",
      "jump": "{turn}번째 턴으로 이동",
      "pageUp": "이전 메시지",
      "pageDown": "최근 메시지"
    }
  }
}
</i18n>
