<template>
  <!-- Copy message button: shown only for user/AI text messages with a non-empty body;
       fades in when the bubble is hovered or keyboard-focused -->
  <button
    type="button"
    :class="[
      'absolute top-2 right-2 flex items-center justify-center w-7 h-7 rounded-full border border-solid cursor-pointer select-none opacity-0 group-hover:opacity-100 focus-visible:opacity-100 transition-opacity duration-200',
      isUser
        ? 'bg-blue-700/40 hover:bg-blue-600/50 border-white/20 text-blue-100 hover:text-white'
        : 'bg-white/70 hover:bg-white border-gray-200 shadow-sm text-gray-400 hover:text-gray-600 dark:border-gray-700 dark:bg-gray-800/60 dark:hover:bg-gray-700 dark:text-gray-400 dark:hover:text-gray-200'
    ]"
    :aria-label="copyLabel"
    :title="copied ? copiedLabel : copyLabel"
    @click="$emit('copy')">
    <span
      :class="[
        'pi text-xs',
        copied ? (isUser ? 'pi-check text-emerald-300' : 'pi-check text-green-600') : 'pi-copy'
      ]"></span>
  </button>
</template>

<script setup lang="ts">
interface Props {
  /** Whether the "copied ✓" feedback is currently shown for this message */
  copied: boolean;
  /** Whether the host message is a USER message (drives the floating-button palette) */
  isUser: boolean;
  /** Localized "Copy" label */
  copyLabel: string;
  /** Localized "Copied" label */
  copiedLabel: string;
}
defineProps<Props>();
defineEmits<{ copy: [] }>();
</script>
