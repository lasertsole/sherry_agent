<template>
  <!-- Floating find bar over the conversation (top-right). It is absolutely
       positioned so it never takes document flow away from the virtual list --
       opening the search must not shift a single row. -->
  <div
    v-if="visible"
    class="find-bar absolute right-3 top-2 z-30 flex items-center gap-1 rounded-lg border border-solid border-gray-200 bg-white px-2 py-1 shadow-md dark:border-gray-700 dark:bg-[#1a1d21]"
    data-test="conversation-find-bar"
    role="search">
    <input
      ref="inputRef"
      v-model="draft"
      type="text"
      class="w-44 min-w-0 bg-transparent text-xs outline-none"
      :class="{ 'text-red-500': hasQuery && total === 0 }"
      :placeholder="t('chatFind.placeholder')"
      :aria-label="t('chatFind.placeholder')"
      data-test="conversation-find-input"
      @input="emit('search', draft)"
      @keydown.enter.exact.prevent="emit('next')"
      @keydown.enter.shift.prevent="emit('previous')"
      @keydown.esc.prevent="emit('close')" />
    <span
      class="shrink-0 font-mono text-[0.7rem] text-gray-500 dark:text-gray-400"
      data-test="conversation-find-count">
      {{ hasQuery ? `${total === 0 ? 0 : currentIndex + 1}/${total}` : '0/0' }}
    </span>
    <button
      type="button"
      class="shrink-0 cursor-pointer rounded p-0.5 text-xs transition-colors hover:bg-gray-100 disabled:opacity-40 dark:hover:bg-gray-800"
      :disabled="total === 0"
      :title="t('chatFind.previous')"
      :aria-label="t('chatFind.previous')"
      data-test="conversation-find-previous"
      @click="emit('previous')">
      <i
        class="pi pi-chevron-up text-[0.65rem]"
        aria-hidden="true"></i>
    </button>
    <button
      type="button"
      class="shrink-0 cursor-pointer rounded p-0.5 text-xs transition-colors hover:bg-gray-100 disabled:opacity-40 dark:hover:bg-gray-800"
      :disabled="total === 0"
      :title="t('chatFind.next')"
      :aria-label="t('chatFind.next')"
      data-test="conversation-find-next"
      @click="emit('next')">
      <i
        class="pi pi-chevron-down text-[0.65rem]"
        aria-hidden="true"></i>
    </button>
    <button
      type="button"
      class="shrink-0 cursor-pointer rounded p-0.5 text-xs transition-colors hover:bg-gray-100 dark:hover:bg-gray-800"
      :title="t('chatFind.close')"
      :aria-label="t('chatFind.close')"
      data-test="conversation-find-close"
      @click="emit('close')">
      <i
        class="pi pi-times text-[0.65rem]"
        aria-hidden="true"></i>
    </button>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';

/**
 * The chat's find bar: a controlled input over `useConversationFind`.
 *
 * It owns ONE thing the composable does not — the draft string it renders —
 * because the input must stay instant while the parent debounces the actual
 * search. Everything else (matches, the current index, visibility) is the
 * caller's state, so the bar never decides what a match is.
 */
const props = defineProps<{
  visible: boolean;
  /** Matches for the query the caller most recently searched. */
  total: number;
  /** Index of the match under the ring (-1 = none). */
  currentIndex: number;
}>();

const emit = defineEmits<{
  /** The query changed (the caller debounces the actual scan). */
  search: [query: string];
  next: [];
  previous: [];
  close: [];
}>();

const { t } = useI18n();

const inputRef = ref<HTMLInputElement>();
/** The input's own text (kept in sync when the caller clears the query). */
const draft = ref('');

/** Whether the box holds a real query (drives the 0/0 vs N/M label). */
const hasQuery = computed(() => draft.value.trim().length > 0);

// Opening (re)focuses the input: Ctrl+Shift+F is a keyboard gesture, so the
// caret must already be where the user will type.
watch(
  () => props.visible,
  async isVisible => {
    if (!isVisible) {
      draft.value = '';
      return;
    }
    await nextTick();
    inputRef.value?.focus();
    inputRef.value?.select();
  }
);
</script>
