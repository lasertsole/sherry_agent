<template>
  <!-- "Back to here" — cuts the conversation at this message. Two-state on
       purpose: the first click arms it, the second confirms, and the armed
       state resets on blur/leave so a stray click cannot cut a conversation. -->
  <button
    v-if="armed || canRewind"
    type="button"
    :disabled="!canRewind || busy"
    :title="disabledTitle"
    :class="[
      'flex items-center gap-1 rounded-full border border-solid px-2.5 py-1 text-xs transition-colors select-none',
      !canRewind || busy
        ? 'cursor-not-allowed border-gray-200 bg-gray-50 text-gray-400 dark:border-gray-700 dark:bg-gray-800/40 dark:text-gray-500'
        : armed
          ? 'border-red-300 bg-red-50 text-red-600 hover:bg-red-100 dark:border-red-800/60 dark:bg-red-950/40 dark:text-red-300'
          : 'cursor-pointer border-gray-200 bg-white text-gray-500 hover:bg-gray-50 dark:border-gray-700 dark:bg-gray-800/60 dark:text-gray-400 dark:hover:bg-gray-700'
    ]"
    @click="onClick"
    @blur="armed = false"
    @mouseleave="armed = false">
    <span class="pi pi-history text-[0.7rem]"></span>
    <span>{{ !canRewind || busy ? disabledLabel : armed ? confirmLabel : label }}</span>
  </button>
</template>

<script setup lang="ts">
import { ref } from 'vue';

interface Props {
  /** Whether the server allows a rewind right now (a turn must not be running) */
  canRewind: boolean;
  /** Whether a rewind is in flight */
  busy: boolean;
  /** Localized "回到这里" label */
  label: string;
  /** Localized armed-state label */
  confirmLabel: string;
  /** Localized disabled-state label */
  disabledLabel: string;
  /** Localized disabled-state tooltip */
  disabledTitle: string;
}
const props = defineProps<Props>();
const emit = defineEmits<{ rewind: [] }>();

const armed = ref(false);

const onClick = (): void => {
  if (!props.canRewind || props.busy) return;
  if (!armed.value) {
    armed.value = true;
    return;
  }
  armed.value = false;
  emit('rewind');
};
</script>
